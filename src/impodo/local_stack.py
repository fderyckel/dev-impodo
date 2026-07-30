"""Status-only discovery for a local Windows Odoo development stack.

This module deliberately exposes no start, stop, shell, PostgreSQL credential,
or arbitrary-command capability.  It reads a user-selected ``odoo.conf`` file,
retains only an allowlisted non-secret subset, and performs bounded readiness
checks against loopback endpoints.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from enum import StrEnum
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


MAX_CONFIG_BYTES = 1024 * 1024
MAX_VERSION_RESPONSE_BYTES = 64 * 1024
LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})


class LocalStackError(ValueError):
    """Raised when local-stack discovery cannot continue safely."""


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


@dataclass(frozen=True, slots=True)
class LocalStackStatus:
    """Current status presented by the local readiness assistant."""

    config_path: str
    base_url: str
    database_hint: str
    checks: tuple[LocalStackCheck, ...]
    profile: LocalStackProfile | None = None

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


class LocalStackService:
    """Hold machine-local, session-lifetime readiness profiles by project."""

    def __init__(
        self,
        *,
        config_picker: ConfigPicker | None = None,
        probe: StackProbe | None = None,
    ) -> None:
        self._config_picker = config_picker or pick_odoo_config
        self._probe = probe or probe_local_stack
        self._statuses: dict[str, LocalStackStatus] = {}

    def pick_config(self) -> Path | None:
        selected = self._config_picker()
        return Path(selected) if selected else None

    def select_config(
        self,
        project_id: str,
        config_path: str | Path,
    ) -> LocalStackStatus:
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
        self._statuses[project_id] = refreshed
        return refreshed

    def get(self, project_id: str) -> LocalStackStatus:
        return self._statuses.get(project_id, LocalStackStatus.unconfigured())


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
            workspace_root / "tools/postgresql/pgsql/bin/pg_isready.exe",
            workspace_root / "tools/postgresql/bin/pg_isready.exe",
        ),
        pg_ctl_path=_first_file(
            workspace_root / "tools/postgresql/pgsql/bin/pg_ctl.exe",
            workspace_root / "tools/postgresql/bin/pg_ctl.exe",
        ),
        pg_data_path=_first_directory(workspace_root / "pgdata"),
        python_path=_first_file(
            workspace_root / "venv/Scripts/python.exe",
            workspace_root / ".venv/Scripts/python.exe",
        ),
        odoo_bin_path=_first_file(
            workspace_root / "odoo/odoo-bin",
            workspace_root / "odoo-bin",
        ),
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


def _first_file(*candidates: Path) -> Path | None:
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _first_directory(*candidates: Path) -> Path | None:
    return next(
        (candidate for candidate in candidates if candidate.is_dir()),
        None,
    )


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
        label="Impodo API",
        level=ReadinessLevel.UNKNOWN,
        message="Use “Save and test connection” after entering the database and API key.",
    )
