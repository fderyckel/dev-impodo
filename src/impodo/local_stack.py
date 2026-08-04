"""Discovery and controlled lifecycle support for a local Windows Odoo stack.

This module exposes fixed PostgreSQL and Odoo start/stop sequences, but no
shell, PostgreSQL credential, arbitrary-command, or external-process control
capability.  It reads a user-selected ``odoo.conf`` file, retains only an
allowlisted non-secret subset, and restricts readiness checks and launched
services to loopback endpoints.  Stop and Restart apply only to exact services
started and retained in memory by the current Impodo process.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
from enum import StrEnum
import json
import os
from pathlib import Path
import socket
import subprocess
from threading import Lock
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


MAX_CONFIG_BYTES = 1024 * 1024
MAX_VERSION_RESPONSE_BYTES = 64 * 1024
POSTGRES_START_TIMEOUT_SECONDS = 15
POSTGRES_STOP_TIMEOUT_SECONDS = 15
ODOO_START_TIMEOUT_SECONDS = 30
ODOO_POLL_INTERVAL_SECONDS = 0.5
LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})
DATABASE_ACCESS_LABEL = "Database access (read-only)"


class LocalStackError(ValueError):
    """Raised when local-stack discovery cannot continue safely."""


class LocalStackStartError(LocalStackError):
    """Startup failed after Impodo had already started one or more services."""

    def __init__(
        self,
        message: str,
        *,
        profile: LocalStackProfile,
        postgresql_pid: int | None,
        odoo_process: ProcessHandle | None = None,
    ) -> None:
        super().__init__(message)
        self.profile = profile
        self.postgresql_pid = postgresql_pid
        self.odoo_process = odoo_process


class ReadinessLevel(StrEnum):
    READY = "ready"
    ACTION = "action"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LocalStackCheck:
    key: str
    label: str
    level: ReadinessLevel
    message: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LocalStackProfile:
    """Non-secret settings derived from one explicitly selected config file."""

    config_path: Path
    workspace_root: Path
    db_host: str
    db_port: int
    db_user: str
    http_interface: str
    http_port: int
    base_url: str
    database_hint: str
    pg_isready_path: Path | None
    pg_ctl_path: Path | None
    pg_data_path: Path | None
    python_path: Path | None
    odoo_bin_path: Path | None
    logs_path: Path | None


@dataclass(frozen=True, slots=True)
class LocalStackStatus:
    """Current status presented by the local readiness assistant."""

    config_path: str
    base_url: str
    database_hint: str
    checks: tuple[LocalStackCheck, ...]
    profile: LocalStackProfile | None = None
    managed_services: tuple[str, ...] = ()

    @property
    def odoo_ready(self) -> bool:
        return self._level("odoo") is ReadinessLevel.READY

    @property
    def startup_needed(self) -> bool:
        return self.profile is not None and not self.odoo_ready

    @property
    def metadata_ready(self) -> bool:
        return self._level("api") is ReadinessLevel.READY

    @property
    def missing_start_requirements(self) -> tuple[str, ...]:
        profile = self.profile
        if profile is None or not self.startup_needed:
            return ()
        requirements: list[tuple[str, Path | None]] = [
            ("Python executable", profile.python_path),
            ("odoo-bin", profile.odoo_bin_path),
        ]
        if self._level("postgresql") is not ReadinessLevel.READY:
            requirements.extend(
                (
                    ("pg_ctl.exe", profile.pg_ctl_path),
                    ("pg_isready.exe", profile.pg_isready_path),
                    ("PostgreSQL data directory", profile.pg_data_path),
                    ("logs directory", profile.logs_path),
                )
            )
        return tuple(label for label, path in requirements if path is None)

    @property
    def can_start(self) -> bool:
        return self.startup_needed and not self.missing_start_requirements

    @property
    def has_managed_services(self) -> bool:
        return bool(self.managed_services)

    def _level(self, key: str) -> ReadinessLevel:
        return next(
            (
                check.level
                for check in self.checks
                if check.key == key
            ),
            ReadinessLevel.UNKNOWN,
        )

    @classmethod
    def unconfigured(cls) -> "LocalStackStatus":
        return cls(
            config_path="",
            base_url="",
            database_hint="",
            checks=(
                LocalStackCheck(
                    key="configuration",
                    label="Configuration",
                    level=ReadinessLevel.UNKNOWN,
                    message="Choose a local odoo.conf file.",
                ),
                _waiting_check(
                    "postgresql",
                    "PostgreSQL",
                    "Waiting for a valid configuration.",
                ),
                _waiting_check(
                    "odoo",
                    "Odoo server",
                    "Waiting for PostgreSQL and Odoo configuration.",
                ),
                _api_check(),
            ),
        )

    @classmethod
    def invalid(cls, config_path: Path, message: str) -> "LocalStackStatus":
        return cls(
            config_path=str(config_path),
            base_url="",
            database_hint="",
            checks=(
                LocalStackCheck(
                    key="configuration",
                    label="Configuration",
                    level=ReadinessLevel.ERROR,
                    message=message,
                ),
                _waiting_check(
                    "postgresql",
                    "PostgreSQL",
                    "Fix the configuration before checking PostgreSQL.",
                ),
                _waiting_check(
                    "odoo",
                    "Odoo server",
                    "Fix the configuration before checking Odoo.",
                ),
                _api_check(),
            ),
        )


class ConfigPicker(Protocol):
    def __call__(self) -> str | Path | None: ...


class StackProbe(Protocol):
    def __call__(self, profile: LocalStackProfile) -> LocalStackStatus: ...


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: int | float | None = None) -> int: ...

    def kill(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LocalStackStartResult:
    status: LocalStackStatus
    odoo_process: ProcessHandle | None
    postgresql_pid: int | None


class StackStarter(Protocol):
    def __call__(self, profile: LocalStackProfile) -> LocalStackStartResult: ...


@dataclass(slots=True)
class _LocalStackOwnership:
    profile: LocalStackProfile
    odoo_process: ProcessHandle | None
    postgresql_pid: int | None

    @property
    def services(self) -> tuple[str, ...]:
        services = []
        if self.odoo_process is not None and self.odoo_process.poll() is None:
            services.append("Odoo")
        if self.postgresql_pid is not None:
            services.append("PostgreSQL")
        return tuple(services)


class LocalStackService:
    """Hold machine-local, session-lifetime readiness profiles by project."""

    def __init__(
        self,
        *,
        config_picker: ConfigPicker | None = None,
        probe: StackProbe | None = None,
        starter: StackStarter | None = None,
    ) -> None:
        self._config_picker = config_picker or pick_odoo_config
        self._probe = probe or probe_local_stack
        self._starter = starter or start_local_stack
        self._statuses: dict[str, LocalStackStatus] = {}
        self._ownership: dict[str, _LocalStackOwnership] = {}
        self._control_lock = Lock()

    def pick_config(self) -> Path | None:
        selected = self._config_picker()
        return Path(selected) if selected else None

    def select_config(
        self,
        project_id: str,
        config_path: str | Path,
    ) -> LocalStackStatus:
        if self._managed_services(project_id):
            raise LocalStackError(
                "Stop the services managed by Impodo before choosing "
                "another Odoo configuration."
            )
        selected = Path(config_path)
        try:
            profile = read_odoo_config(selected)
            status = self._probe(profile)
        except LocalStackError as error:
            status = LocalStackStatus.invalid(selected, str(error))
        self._statuses[project_id] = status
        return status

    def refresh(self, project_id: str) -> LocalStackStatus:
        current = self.get(project_id)
        if current.profile is None:
            return current
        try:
            profile = read_odoo_config(current.profile.config_path)
            refreshed = self._probe(profile)
        except LocalStackError as error:
            refreshed = LocalStackStatus.invalid(
                current.profile.config_path,
                str(error),
            )
        return self._store_status(project_id, refreshed)

    def start(self, project_id: str) -> LocalStackStatus:
        """Reread the live config and start only the missing local services."""

        with self._control_lock:
            return self._start_locked(project_id)

    def _start_locked(self, project_id: str) -> LocalStackStatus:
        if self._managed_services(project_id):
            raise LocalStackError(
                "Impodo already manages local services for this project. "
                "Use Restart to cycle them safely."
            )
        current = self.get(project_id)
        if current.profile is None:
            raise LocalStackError("Choose and validate odoo.conf before starting.")
        try:
            profile = read_odoo_config(current.profile.config_path)
        except LocalStackError as error:
            self._statuses[project_id] = LocalStackStatus.invalid(
                current.profile.config_path,
                str(error),
            )
            raise
        try:
            result = self._starter(profile)
        except LocalStackStartError as error:
            ownership = _LocalStackOwnership(
                profile=error.profile,
                odoo_process=error.odoo_process,
                postgresql_pid=error.postgresql_pid,
            )
            if ownership.services:
                self._ownership[project_id] = ownership
            self._store_status(project_id, self._probe(profile))
            raise
        except LocalStackError:
            self._store_status(project_id, self._probe(profile))
            raise
        ownership = _LocalStackOwnership(
            profile=profile,
            odoo_process=result.odoo_process,
            postgresql_pid=result.postgresql_pid,
        )
        if ownership.services:
            self._ownership[project_id] = ownership
        else:
            self._ownership.pop(project_id, None)
        return self._store_status(project_id, result.status)

    def stop(self, project_id: str) -> LocalStackStatus:
        """Stop only services started by this Impodo process."""

        with self._control_lock:
            return self._stop_locked(project_id)

    def _stop_locked(self, project_id: str) -> LocalStackStatus:
        ownership = self._ownership.get(project_id)
        if ownership is None or not ownership.services:
            self._ownership.pop(project_id, None)
            raise LocalStackError(
                "Impodo does not own any running service for this project. "
                "Existing external processes were not changed."
            )

        if ownership.odoo_process is not None:
            _stop_owned_odoo(ownership.odoo_process)
            ownership.odoo_process = None
            if not _wait_for_loopback_port_closed(
                ownership.profile.http_interface,
                ownership.profile.http_port,
            ):
                status = self._probe(ownership.profile)
                self._store_status(project_id, status)
                raise LocalStackError(
                    "The Impodo-managed Odoo process stopped, but another "
                    "listener remains on the configured Odoo port. "
                    "PostgreSQL was not stopped."
                )

        if ownership.postgresql_pid is not None:
            try:
                _stop_postgresql(
                    ownership.profile,
                    expected_pid=ownership.postgresql_pid,
                )
            except LocalStackError:
                self._store_status(project_id, self._probe(ownership.profile))
                raise
            ownership.postgresql_pid = None

        self._ownership.pop(project_id, None)
        return self._store_status(
            project_id,
            self._probe(ownership.profile),
        )

    def restart(self, project_id: str) -> LocalStackStatus:
        """Restart only a stack currently owned by this Impodo process."""

        with self._control_lock:
            if not self._managed_services(project_id):
                raise LocalStackError(
                    "Impodo can restart only services it started during "
                    "this session. Existing external processes were not changed."
                )
            self._stop_locked(project_id)
            return self._start_locked(project_id)

    def get(self, project_id: str) -> LocalStackStatus:
        status = self._statuses.get(project_id, LocalStackStatus.unconfigured())
        return self._decorate_status(project_id, status)

    def mark_metadata_ready(
        self,
        project_id: str,
        *,
        database: str,
        odoo_version: str,
        model_count: int,
    ) -> LocalStackStatus:
        """Record successful Odoo metadata access for this local session."""

        current = self.get(project_id)
        if current.profile is None:
            raise LocalStackError(
                "Choose and validate odoo.conf before verifying metadata access."
            )
        verified = LocalStackCheck(
            key="api",
            label=DATABASE_ACCESS_LABEL,
            level=ReadinessLevel.READY,
            message="Odoo metadata read succeeded.",
            detail=(
                f"{database}; Odoo {odoo_version}; "
                f"{model_count} persistent model(s)"
            ),
        )
        updated = replace(
            current,
            checks=tuple(
                verified if check.key == "api" else check
                for check in current.checks
            ),
        )
        return self._store_status(project_id, updated)

    def mark_connection_ready(
        self,
        project_id: str,
        *,
        database: str,
        odoo_version: str,
    ) -> LocalStackStatus:
        """Record successful read-only database access for this session."""

        current = self.get(project_id)
        verified = LocalStackCheck(
            key="api",
            label=DATABASE_ACCESS_LABEL,
            level=ReadinessLevel.READY,
            message="Read-only database access succeeded.",
            detail=f"{database}; Odoo {odoo_version}",
        )
        updated = replace(
            current,
            checks=tuple(
                verified if check.key == "api" else check
                for check in current.checks
            ),
        )
        return self._store_status(project_id, updated)

    def mark_connection_error(
        self,
        project_id: str,
        *,
        detail: str,
    ) -> LocalStackStatus:
        """Record a definitive failed local connection test."""

        current = self.get(project_id)
        failed = LocalStackCheck(
            key="api",
            label=DATABASE_ACCESS_LABEL,
            level=ReadinessLevel.ERROR,
            message="Read-only database access failed.",
            detail=detail,
        )
        updated = replace(
            current,
            checks=tuple(
                failed
                if check.key == "api"
                else (
                    check
                    if check.level is ReadinessLevel.READY
                    else replace(check, level=ReadinessLevel.ERROR)
                )
                for check in current.checks
            ),
        )
        return self._store_status(project_id, updated)

    def _managed_services(self, project_id: str) -> tuple[str, ...]:
        ownership = self._ownership.get(project_id)
        if ownership is None:
            return ()
        services = ownership.services
        if not services:
            self._ownership.pop(project_id, None)
        return services

    def _decorate_status(
        self,
        project_id: str,
        status: LocalStackStatus,
    ) -> LocalStackStatus:
        return replace(
            status,
            managed_services=self._managed_services(project_id),
        )

    def _store_status(
        self,
        project_id: str,
        status: LocalStackStatus,
    ) -> LocalStackStatus:
        decorated = self._decorate_status(project_id, status)
        self._statuses[project_id] = decorated
        return decorated


def pick_odoo_config() -> Path | None:
    """Open a native file picker without uploading or copying the config."""

    if os.name != "nt":
        raise LocalStackError(
            "The native odoo.conf picker is currently available on Windows only."
        )
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError as error:
        raise LocalStackError(
            "The Windows configuration picker is unavailable in this Python build."
        ) from error

    root = None
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(
            parent=root,
            title="Choose the local Odoo configuration",
            filetypes=(
                ("Odoo configuration", "*.conf"),
                ("All files", "*.*"),
            ),
        )
    except tkinter.TclError as error:
        raise LocalStackError(
            "Windows could not open the Odoo configuration picker."
        ) from error
    finally:
        if root is not None:
            root.destroy()
    return Path(selected) if selected else None


def read_odoo_config(config_path: str | Path) -> LocalStackProfile:
    """Read only safe routing settings from one selected Odoo config."""

    try:
        selected = Path(config_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LocalStackError(
            "Choose an existing .conf Odoo configuration file."
        ) from error
    if not selected.is_file() or selected.suffix.casefold() != ".conf":
        raise LocalStackError("Choose an existing .conf Odoo configuration file.")
    try:
        size = selected.stat().st_size
    except OSError as error:
        raise LocalStackError("The selected configuration cannot be inspected.") from error
    if size > MAX_CONFIG_BYTES:
        raise LocalStackError("The selected configuration is unexpectedly large.")

    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with selected.open("r", encoding="utf-8-sig") as stream:
            parser.read_file(stream)
    except (OSError, UnicodeError, configparser.Error) as error:
        raise LocalStackError(
            "The selected file is not a readable Odoo configuration."
        ) from error
    if not parser.has_section("options"):
        raise LocalStackError("The selected file has no [options] section.")

    http_enabled = _option(parser, "http_enable", "true").casefold()
    if http_enabled in {"0", "false", "no", "off"}:
        raise LocalStackError("Odoo HTTP is disabled in the selected configuration.")
    http_interface = _option(parser, "http_interface", "")
    if http_interface.casefold() not in LOOPBACK_NAMES:
        raise LocalStackError(
            "Local Odoo must bind explicitly to 127.0.0.1, ::1, or localhost."
        )
    db_host = _option(parser, "db_host", "127.0.0.1")
    if db_host.casefold() not in LOOPBACK_NAMES:
        raise LocalStackError(
            "The local assistant supports only loopback PostgreSQL hosts."
        )

    db_port = _port(_option(parser, "db_port", "5432"), "PostgreSQL")
    http_port = _port(_option(parser, "http_port", "8069"), "Odoo HTTP")
    db_user = _option(parser, "db_user", "")
    database_hint = _database_hint(_option(parser, "db_name", ""))
    workspace_root = _workspace_root(selected)
    base_host = "[::1]" if http_interface == "::1" else "127.0.0.1"

    return LocalStackProfile(
        config_path=selected,
        workspace_root=workspace_root,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        http_interface=http_interface,
        http_port=http_port,
        base_url=f"http://{base_host}:{http_port}",
        database_hint=database_hint,
        pg_isready_path=_first_file(
            workspace_root,
            workspace_root / "tools/postgresql/pgsql/bin/pg_isready.exe",
            workspace_root / "tools/postgresql/bin/pg_isready.exe",
        ),
        pg_ctl_path=_first_file(
            workspace_root,
            workspace_root / "tools/postgresql/pgsql/bin/pg_ctl.exe",
            workspace_root / "tools/postgresql/bin/pg_ctl.exe",
        ),
        pg_data_path=_first_directory(workspace_root, workspace_root / "pgdata"),
        python_path=_first_file(
            workspace_root,
            workspace_root / "venv/Scripts/python.exe",
            workspace_root / ".venv/Scripts/python.exe",
        ),
        odoo_bin_path=_first_file(
            workspace_root,
            workspace_root / "odoo/odoo-bin",
            workspace_root / "odoo-bin",
        ),
        logs_path=_first_directory(workspace_root, workspace_root / "logs"),
    )


def probe_local_stack(profile: LocalStackProfile) -> LocalStackStatus:
    """Perform bounded, read-only readiness checks for one safe profile."""

    configuration = LocalStackCheck(
        key="configuration",
        label="Configuration",
        level=ReadinessLevel.READY,
        message="Valid loopback Odoo configuration.",
        detail=(
            f"PostgreSQL {profile.db_host}:{profile.db_port}; "
            f"Odoo {profile.base_url}"
        ),
    )
    return LocalStackStatus(
        config_path=str(profile.config_path),
        base_url=profile.base_url,
        database_hint=profile.database_hint,
        checks=(
            configuration,
            _probe_postgresql(profile),
            _probe_odoo(profile),
            _api_check(),
        ),
        profile=profile,
    )


def start_local_stack(profile: LocalStackProfile) -> LocalStackStartResult:
    """Start missing services in the fixed PostgreSQL-then-Odoo order."""

    if os.name != "nt":
        raise LocalStackError(
            "Controlled local-stack startup is currently available on Windows only."
        )
    initial = probe_local_stack(profile)
    if initial.odoo_ready:
        return LocalStackStartResult(
            status=initial,
            odoo_process=None,
            postgresql_pid=None,
        )
    if initial.missing_start_requirements:
        missing = ", ".join(initial.missing_start_requirements)
        raise LocalStackError(f"Cannot start the local stack; not found: {missing}.")

    postgresql = _check(initial, "postgresql")
    odoo = _check(initial, "odoo")
    postgresql_pid: int | None = None
    odoo_process: ProcessHandle | None = None
    if odoo.level is ReadinessLevel.ERROR:
        raise LocalStackError(
            "The configured Odoo port answered unexpectedly. "
            "Resolve that listener before starting another Odoo process."
        )
    if postgresql.level is not ReadinessLevel.READY:
        if postgresql.level is not ReadinessLevel.ACTION:
            raise LocalStackError(
                "PostgreSQL readiness could not be established safely."
            )
        postgresql_pid = _start_postgresql(profile)
        postgresql = _probe_postgresql(profile)
        if postgresql.level is not ReadinessLevel.READY:
            raise LocalStackStartError(
                "PostgreSQL started but is not accepting connections on "
                "the configured loopback port. Odoo was not started.",
                profile=profile,
                postgresql_pid=postgresql_pid,
            )

    try:
        odoo_process = _start_odoo(profile)
    except LocalStackStartError as error:
        raise LocalStackStartError(
            str(error),
            profile=profile,
            postgresql_pid=postgresql_pid or error.postgresql_pid,
            odoo_process=error.odoo_process,
        ) from error
    except LocalStackError as error:
        if postgresql_pid is not None:
            raise LocalStackStartError(
                str(error),
                profile=profile,
                postgresql_pid=postgresql_pid,
            ) from error
        raise
    final = probe_local_stack(profile)
    if (
        _check(final, "postgresql").level is not ReadinessLevel.READY
        or _check(final, "odoo").level is not ReadinessLevel.READY
    ):
        raise LocalStackStartError(
            "The startup sequence completed, but the final readiness check "
            "did not stay green. Check the service status again.",
            profile=profile,
            postgresql_pid=postgresql_pid,
            odoo_process=odoo_process,
        )
    return LocalStackStartResult(
        status=final,
        odoo_process=odoo_process,
        postgresql_pid=postgresql_pid,
    )


def _start_postgresql(profile: LocalStackProfile) -> int:
    pg_ctl_path = _required_path(profile.pg_ctl_path, "pg_ctl.exe")
    pg_data_path = _required_path(
        profile.pg_data_path,
        "PostgreSQL data directory",
    )
    logs_path = _required_path(profile.logs_path, "logs directory")
    status = _run_control_command(
        [
            str(pg_ctl_path),
            "status",
            "-D",
            str(pg_data_path),
        ],
        cwd=profile.workspace_root,
        timeout=5,
    )
    if status.returncode == 0:
        raise LocalStackError(
            "A PostgreSQL process already uses the selected data directory, "
            "but it is not ready on the configured loopback port."
        )
    if status.returncode != 3:
        raise LocalStackError(
            "PostgreSQL status could not be established for the selected "
            "data directory."
        )

    options = f"-h {_probe_host(profile.db_host)} -p {profile.db_port}"
    started = _run_control_command(
        [
            str(pg_ctl_path),
            "start",
            "-D",
            str(pg_data_path),
            "-l",
            str(logs_path / "postgresql.log"),
            "-o",
            options,
            "-w",
            "-t",
            str(POSTGRES_START_TIMEOUT_SECONDS),
        ],
        cwd=profile.workspace_root,
        timeout=POSTGRES_START_TIMEOUT_SECONDS + 5,
    )
    if started.returncode != 0:
        raise LocalStackError(
            "PostgreSQL did not start successfully. Review logs/postgresql.log."
        )
    try:
        return _read_postgresql_pid(profile)
    except LocalStackError as error:
        raise LocalStackError(
            "PostgreSQL started, but Impodo could not bind ownership to its "
            "postmaster PID. Odoo was not started; use the workspace status "
            "and shutdown procedure."
        ) from error


def _start_odoo(profile: LocalStackProfile) -> ProcessHandle:
    python_path = _required_path(profile.python_path, "Python executable")
    odoo_bin_path = _required_path(profile.odoo_bin_path, "odoo-bin")
    command = [
        str(python_path),
        str(odoo_bin_path),
        "-c",
        str(profile.config_path),
    ]
    environment = os.environ.copy()
    for name in ("PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE"):
        environment.pop(name, None)
    creation_flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        process = subprocess.Popen(
            command,
            close_fds=True,
            creationflags=creation_flags,
            cwd=str(profile.workspace_root),
            env=environment,
            shell=False,
        )
    except OSError as error:
        raise LocalStackError(
            "Windows could not launch the detected Odoo Python process."
        ) from error

    deadline = time.monotonic() + ODOO_START_TIMEOUT_SECONDS
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise LocalStackError(
                "The Odoo process exited before its HTTP endpoint became ready. "
                "Review the Odoo console window."
            )
        odoo = _probe_odoo(profile)
        if odoo.level is ReadinessLevel.READY:
            return process
        if (
            odoo.level is ReadinessLevel.ERROR
            and odoo.message.startswith("Expected Odoo 19")
        ):
            try:
                _stop_owned_odoo(process)
            except LocalStackError as stop_error:
                raise LocalStackStartError(
                    "Another Odoo version answered on the configured port, "
                    "and the newly launched Odoo process could not be stopped.",
                    profile=profile,
                    postgresql_pid=None,
                    odoo_process=process,
                ) from stop_error
            raise LocalStackError(
                "Another Odoo version answered on the configured port; "
                "the newly launched process was stopped."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                _stop_owned_odoo(process)
            except LocalStackError as stop_error:
                raise LocalStackStartError(
                    "Odoo did not become ready within 30 seconds, and the "
                    "newly launched Odoo process could not be stopped.",
                    profile=profile,
                    postgresql_pid=None,
                    odoo_process=process,
                ) from stop_error
            raise LocalStackError(
                "Odoo did not become ready within 30 seconds; the newly "
                "launched process was stopped. Review the Odoo console."
            )
        time.sleep(min(ODOO_POLL_INTERVAL_SECONDS, remaining))


def _stop_owned_odoo(process: ProcessHandle) -> None:
    """Stop only the exact Odoo child handle retained by Impodo."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LocalStackError(
                "The Impodo-managed Odoo process could not be stopped."
            ) from error
    except OSError as error:
        if process.poll() is None:
            raise LocalStackError(
                "The Impodo-managed Odoo process could not be stopped."
            ) from error


def _stop_postgresql(
    profile: LocalStackProfile,
    *,
    expected_pid: int,
) -> None:
    """Stop the exact PostgreSQL postmaster previously started by Impodo."""

    pg_ctl_path = _required_path(
        profile.pg_ctl_path,
        "pg_ctl.exe",
        operation="stop",
    )
    pg_data_path = _required_path(
        profile.pg_data_path,
        "PostgreSQL data directory",
        operation="stop",
    )
    status_command = [
        str(pg_ctl_path),
        "status",
        "-D",
        str(pg_data_path),
    ]
    status = _run_control_command(
        status_command,
        cwd=profile.workspace_root,
        timeout=5,
    )
    if status.returncode == 3:
        return
    if status.returncode != 0:
        raise LocalStackError(
            "PostgreSQL status could not be established for the "
            "Impodo-managed data directory."
        )
    actual_pid = _read_postgresql_pid(profile)
    if actual_pid != expected_pid:
        raise LocalStackError(
            "PostgreSQL was not stopped because the server identity changed "
            f"from PID {expected_pid} to PID {actual_pid}. Use the workspace "
            "status and shutdown procedure."
        )

    stopped = _run_control_command(
        [
            str(pg_ctl_path),
            "stop",
            "-D",
            str(pg_data_path),
            "-m",
            "fast",
            "-w",
            "-t",
            str(POSTGRES_STOP_TIMEOUT_SECONDS),
        ],
        cwd=profile.workspace_root,
        timeout=POSTGRES_STOP_TIMEOUT_SECONDS + 5,
    )
    if stopped.returncode != 0:
        raise LocalStackError(
            "PostgreSQL did not stop cleanly within the safety timeout."
        )
    verified = _run_control_command(
        status_command,
        cwd=profile.workspace_root,
        timeout=5,
    )
    if verified.returncode != 3:
        raise LocalStackError(
            "PostgreSQL stop returned, but the selected data directory "
            "still reports a running server."
        )


def _read_postgresql_pid(profile: LocalStackProfile) -> int:
    """Read and validate the postmaster PID for the selected data directory."""

    data_path = _required_path(
        profile.pg_data_path,
        "PostgreSQL data directory",
    )
    pid_path = data_path / "postmaster.pid"
    try:
        with pid_path.open("r", encoding="ascii") as stream:
            first_line = stream.readline(32)
        pid = int(first_line.strip())
    except (OSError, UnicodeError, ValueError) as error:
        raise LocalStackError(
            "The PostgreSQL postmaster PID could not be read safely."
        ) from error
    if pid <= 0 or pid > 4_294_967_295:
        raise LocalStackError(
            "The PostgreSQL postmaster PID is invalid."
        )
    return pid


def _loopback_port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((_probe_host(host), port), timeout=1):
            return True
    except (OSError, TimeoutError):
        return False


def _wait_for_loopback_port_closed(
    host: str,
    port: int,
    *,
    timeout: int = 5,
) -> bool:
    deadline = time.monotonic() + timeout
    while _loopback_port_is_open(host, port):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.2, remaining))
    return True


def _run_control_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=str(cwd),
            shell=False,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LocalStackError(
            "A fixed local-stack control command could not complete."
        ) from error


def _check(status: LocalStackStatus, key: str) -> LocalStackCheck:
    return next(
        check
        for check in status.checks
        if check.key == key
    )


def _required_path(
    path: Path | None,
    label: str,
    *,
    operation: str = "start",
) -> Path:
    if path is None:
        raise LocalStackError(
            f"Cannot {operation} the local stack; not found: {label}."
        )
    return path


def _probe_postgresql(profile: LocalStackProfile) -> LocalStackCheck:
    if profile.pg_isready_path is not None:
        command = [
            str(profile.pg_isready_path),
            "-h",
            _probe_host(profile.db_host),
            "-p",
            str(profile.db_port),
            "-t",
            "3",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                shell=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return LocalStackCheck(
                key="postgresql",
                label="PostgreSQL",
                level=ReadinessLevel.ERROR,
                message="The PostgreSQL readiness check failed to run.",
            )
        if completed.returncode == 0:
            return LocalStackCheck(
                key="postgresql",
                label="PostgreSQL",
                level=ReadinessLevel.READY,
                message="PostgreSQL is accepting connections.",
                detail=f"{profile.db_host}:{profile.db_port}",
            )
        if completed.returncode in {1, 2}:
            return LocalStackCheck(
                key="postgresql",
                label="PostgreSQL",
                level=ReadinessLevel.ACTION,
                message="PostgreSQL is not ready yet.",
                detail=f"No ready server at {profile.db_host}:{profile.db_port}",
            )
        return LocalStackCheck(
            key="postgresql",
            label="PostgreSQL",
            level=ReadinessLevel.ERROR,
            message="PostgreSQL readiness could not be determined.",
        )

    try:
        with socket.create_connection(
            (_probe_host(profile.db_host), profile.db_port),
            timeout=2,
        ):
            pass
    except (OSError, TimeoutError):
        return LocalStackCheck(
            key="postgresql",
            label="PostgreSQL",
            level=ReadinessLevel.ACTION,
            message="No PostgreSQL listener was detected.",
            detail="pg_isready.exe was not found in the detected workspace.",
        )
    return LocalStackCheck(
        key="postgresql",
        label="PostgreSQL",
        level=ReadinessLevel.ACTION,
        message="The database port is open, but PostgreSQL is not verified.",
        detail="Install or identify pg_isready.exe to confirm readiness.",
    )


def _probe_odoo(profile: LocalStackProfile) -> LocalStackCheck:
    request_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {},
            "id": 1,
        }
    ).encode("utf-8")
    request = Request(
        f"{profile.base_url}/web/webclient/version_info",
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Impodo local readiness assistant",
        },
        method="POST",
    )
    try:
        with _open_loopback(request, timeout=4) as response:
            payload = response.read(MAX_VERSION_RESPONSE_BYTES + 1)
    except HTTPError:
        return LocalStackCheck(
            key="odoo",
            label="Odoo server",
            level=ReadinessLevel.ERROR,
            message="The HTTP listener did not expose Odoo version information.",
        )
    except (OSError, TimeoutError, URLError):
        return LocalStackCheck(
            key="odoo",
            label="Odoo server",
            level=ReadinessLevel.ACTION,
            message="Odoo is not responding yet.",
            detail=profile.base_url,
        )
    if len(payload) > MAX_VERSION_RESPONSE_BYTES:
        return LocalStackCheck(
            key="odoo",
            label="Odoo server",
            level=ReadinessLevel.ERROR,
            message="The Odoo version response exceeded the safety limit.",
        )
    try:
        version_payload = json.loads(payload.decode("utf-8"))
        version = str(version_payload["result"]["server_version"])
    except (KeyError, TypeError, UnicodeError, ValueError):
        return LocalStackCheck(
            key="odoo",
            label="Odoo server",
            level=ReadinessLevel.ERROR,
            message="The HTTP listener did not return a valid Odoo response.",
        )
    if not version.startswith("19."):
        return LocalStackCheck(
            key="odoo",
            label="Odoo server",
            level=ReadinessLevel.ERROR,
            message=f"Expected Odoo 19, received Odoo {version}.",
        )
    return LocalStackCheck(
        key="odoo",
        label="Odoo server",
        level=ReadinessLevel.READY,
        message=f"Odoo {version} is responding.",
        detail=profile.base_url,
    )


def _option(
    parser: configparser.ConfigParser,
    name: str,
    default: str,
) -> str:
    value = parser.get("options", name, fallback=default)
    cleaned = value.strip()
    if cleaned.casefold() in {"false", "none"} and name in {"db_host", "db_name"}:
        return default
    return cleaned


def _port(value: str, label: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise LocalStackError(f"{label} port is not a valid number.") from error
    if port < 1 or port > 65535:
        raise LocalStackError(f"{label} port must be between 1 and 65535.")
    return port


def _database_hint(value: str) -> str:
    if value and all(character.isalnum() or character in "._-" for character in value):
        return value
    return ""


def _workspace_root(config_path: Path) -> Path:
    parent = config_path.parent
    return parent.parent if parent.name.casefold() == "config" else parent


def _first_file(root: Path, *candidates: Path) -> Path | None:
    resolved_root = root.resolve(strict=True)
    for candidate in candidates:
        try:
            if candidate.is_file():
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(resolved_root)
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _first_directory(root: Path, *candidates: Path) -> Path | None:
    resolved_root = root.resolve(strict=True)
    for candidate in candidates:
        try:
            if candidate.is_dir():
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(resolved_root)
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _probe_host(value: str) -> str:
    return "127.0.0.1" if value.casefold() == "localhost" else value


def _open_loopback(request: Request, *, timeout: int):
    """Open one loopback request without inheriting system proxy settings."""

    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def _waiting_check(key: str, label: str, message: str) -> LocalStackCheck:
    return LocalStackCheck(
        key=key,
        label=label,
        level=ReadinessLevel.UNKNOWN,
        message=message,
    )


def _api_check() -> LocalStackCheck:
    return LocalStackCheck(
        key="api",
        label=DATABASE_ACCESS_LABEL,
        level=ReadinessLevel.UNKNOWN,
        message=(
            "Select the target database, then use Save and test connection. "
            "Local mode does not require an Odoo API key."
        ),
    )
