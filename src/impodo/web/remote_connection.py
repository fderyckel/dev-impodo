"""Present purpose-bound, non-secret Odoo connection status.

The connection check is operational session state, not durable migration
evidence.  Results remain in memory for the lifetime of the local Impodo
process and are accepted only while they match the project's exact target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from threading import RLock

from ..connectors import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
)
from ..models import OdooReadIdentity, TargetFingerprint, target_identity_hash
from ..application.odoo_connection_service import OdooConnectionPurpose
from ..projects import MigrationProject
from ..secrets import SecretStoreError


class RemoteConnectionLevel(str, Enum):
    """Visual and semantic level for one Remote Odoo check."""

    READY = "ready"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RemoteConnectionCheck:
    """One safe, user-facing Remote Odoo connection check."""

    key: str
    label: str
    level: RemoteConnectionLevel
    message: str
    waiting_message: str


@dataclass(frozen=True, slots=True)
class RemoteConnectionStatus:
    """Current process-session result for one exact Remote Odoo target."""

    target_hash: str
    checked_at: str | None
    checks: tuple[RemoteConnectionCheck, ...]
    support_code: str | None = None
    read_principal_hash: str | None = None

    @property
    def checked(self) -> bool:
        """Whether a connection attempt produced this status."""

        return self.checked_at is not None

    @property
    def ready(self) -> bool:
        """Whether every required Remote Odoo check succeeded."""

        return self.checked and all(
            check.level is RemoteConnectionLevel.READY for check in self.checks
        )

    @property
    def has_error(self) -> bool:
        """Whether at least one required Remote Odoo check failed."""

        return any(
            check.level is RemoteConnectionLevel.ERROR for check in self.checks
        )

    @property
    def state(self) -> str:
        """Return the stable presentation state for the status panel."""

        if self.ready:
            return "ready"
        if self.has_error:
            return "error"
        return "unknown"

    @property
    def state_label(self) -> str:
        """Return a text label so state is never communicated by colour alone."""

        if self.ready:
            return "Ready"
        if self.has_error:
            return "Not ready"
        return "Not checked"

    @property
    def heading(self) -> str:
        """Return the primary result sentence."""

        if self.ready:
            return "The Odoo connection is ready."
        if self.has_error:
            return "The Odoo connection is not ready."
        return "The Odoo connection has not been checked."

    @property
    def guidance(self) -> str:
        """Return the next useful instruction without exposing internals."""

        if self.ready:
            return (
                "Impodo confirmed this exact remote target. "
                "Nothing was changed in Odoo."
            )
        if self.has_error:
            return (
                "Review the failed check and try again. "
                "Nothing was changed in Odoo."
            )
        return "Check the connection before continuing."

    @property
    def button_label(self) -> str:
        """Return a result-aware label for the connection action."""

        if self.ready:
            return "Check again"
        if self.has_error:
            return "Try again"
        return "Check connection"


class RemoteConnectionStatusService:
    """Keep bounded, identity-bound Odoo check status in process memory."""

    def __init__(self) -> None:
        self._statuses: dict[tuple[str, OdooConnectionPurpose], RemoteConnectionStatus] = {}
        self._lock = RLock()

    def get(
        self,
        project: MigrationProject,
        purpose: OdooConnectionPurpose = OdooConnectionPurpose.TARGET_READ,
    ) -> RemoteConnectionStatus:
        """Return a status only when it belongs to the project's exact target."""

        expected_hash = _project_target_hash(project)
        key = (project.project_id, purpose)
        with self._lock:
            status = self._statuses.get(key)
            if status is not None and status.target_hash == expected_hash:
                return status
            self._statuses.pop(key, None)
        return _unchecked_status(expected_hash)

    def clear(
        self,
        project_id: str,
        purpose: OdooConnectionPurpose | None = None,
    ) -> None:
        """Remove an earlier result after credentials or target details change."""

        with self._lock:
            if purpose is not None:
                self._statuses.pop((project_id, purpose), None)
            else:
                for key in tuple(self._statuses):
                    if key[0] == project_id:
                        self._statuses.pop(key, None)

    def mark_checked(
        self,
        project: MigrationProject,
        fingerprint: TargetFingerprint,
        identity: OdooReadIdentity,
        purpose: OdooConnectionPurpose = OdooConnectionPurpose.TARGET_READ,
    ) -> RemoteConnectionStatus:
        """Record one successful read, principal probe, and target/version."""

        expected_hash = _project_target_hash(project)
        target_matches = (
            fingerprint.target_hash == expected_hash
            and identity.target_hash == expected_hash
            and fingerprint.connection_mode.strip().upper()
            == (
                project.odoo_connection_mode.value
                if project.odoo_connection_mode is not None
                else ""
            )
            and fingerprint.database == project.odoo_database
        )
        if not target_matches:
            status = _status(
                target_hash=expected_hash,
                server=(
                    RemoteConnectionLevel.READY,
                    "Odoo responded to the read-only check.",
                ),
                database=(
                    RemoteConnectionLevel.ERROR,
                    "Odoo answered for a different target. Review the address and database name.",
                ),
                version=(
                    RemoteConnectionLevel.UNKNOWN,
                    "Waiting for access to the exact database.",
                ),
                principal=(
                    RemoteConnectionLevel.UNKNOWN,
                    "Waiting for access to the exact database.",
                ),
                checked_at=_now(),
                support_code="REMOTE_TARGET_MISMATCH",
            )
        elif not fingerprint.odoo_version.startswith("19."):
            reported = fingerprint.odoo_version or "unknown"
            message = (
                "Impodo could not confirm that this target runs Odoo 19."
                if reported == "unknown"
                else f"Impodo requires Odoo 19; this target reported Odoo {reported}."
            )
            status = _status(
                target_hash=expected_hash,
                server=(
                    RemoteConnectionLevel.READY,
                    "Odoo responded to the read-only check.",
                ),
                database=(
                    RemoteConnectionLevel.READY,
                    f"Read-only access to {project.odoo_database} succeeded.",
                ),
                version=(RemoteConnectionLevel.ERROR, message),
                principal=(
                    RemoteConnectionLevel.READY,
                    "The read-only Odoo principal was identified.",
                ),
                checked_at=fingerprint.snapshot_timestamp or _now(),
                support_code=(
                    "ODOO_VERSION_UNKNOWN"
                    if reported == "unknown"
                    else "ODOO_VERSION_UNSUPPORTED"
                ),
            )
        else:
            status = _status(
                target_hash=expected_hash,
                server=(
                    RemoteConnectionLevel.READY,
                    "Odoo responded to the read-only check.",
                ),
                database=(
                    RemoteConnectionLevel.READY,
                    f"Read-only access to {project.odoo_database} succeeded.",
                ),
                version=(
                    RemoteConnectionLevel.READY,
                    f"Supported Odoo version {fingerprint.odoo_version}.",
                ),
                principal=(
                    RemoteConnectionLevel.READY,
                    "The authenticated read-only Odoo principal was identified.",
                ),
                checked_at=fingerprint.snapshot_timestamp or _now(),
                read_principal_hash=identity.principal_hash,
            )
        with self._lock:
            self._statuses[(project.project_id, purpose)] = status
        return status

    def mark_error(
        self,
        project: MigrationProject,
        error: Exception,
        purpose: OdooConnectionPurpose = OdooConnectionPurpose.TARGET_READ,
    ) -> RemoteConnectionStatus:
        """Record a safely classified failure without persisting raw responses."""

        target_hash = _project_target_hash(project)
        unknown = RemoteConnectionLevel.UNKNOWN
        ready = RemoteConnectionLevel.READY
        failed = RemoteConnectionLevel.ERROR
        if isinstance(error, SecretStoreError):
            values = (
                (unknown, "The Odoo server was not contacted."),
                (failed, "Enter an Odoo access key for this remote target."),
                (unknown, "Waiting for database access."),
                (unknown, "Waiting for an authenticated principal check."),
                "ODOO_ACCESS_KEY_MISSING",
            )
        elif isinstance(error, ConnectorAuthenticationError):
            values = (
                (ready, "Odoo responded to the read-only check."),
                (
                    failed,
                    "Odoo rejected the access key, database name, or API entitlement.",
                ),
                (unknown, "Waiting for database access."),
                (unknown, "Waiting for an authenticated principal check."),
                "ODOO_ACCESS_REJECTED",
            )
        elif isinstance(error, ConnectorAuthorizationError):
            values = (
                (ready, "Odoo responded to the read-only check."),
                (ready, "Authenticated database access succeeded."),
                (unknown, "Waiting for the Odoo version check."),
                (
                    failed,
                    "The authenticated principal lacks required model read access.",
                ),
                "ODOO_READ_ACCESS_MISSING",
            )
        elif isinstance(error, ConnectorConfigurationError):
            values = (
                (failed, "Review the Odoo web address and database name."),
                (unknown, "Waiting for the Odoo server check."),
                (unknown, "Waiting for database access."),
                (unknown, "Waiting for an authenticated principal check."),
                "ODOO_CONNECTION_DETAILS_INVALID",
            )
        elif isinstance(error, ConnectorTransportError):
            status_code = _http_status(error)
            if status_code is None:
                values = (
                    (
                        failed,
                        "Impodo could not reach Odoo. Check the address and network connection.",
                    ),
                    (unknown, "Waiting for the Odoo server check."),
                    (unknown, "Waiting for database access."),
                    (unknown, "Waiting for an authenticated principal check."),
                    "ODOO_UNREACHABLE",
                )
            else:
                database_message = (
                    "The JSON-2 API was not available at this address."
                    if status_code == 404
                    else (
                        "Odoo could not complete the read-only database check "
                        f"(HTTP {status_code})."
                    )
                )
                values = (
                    (ready, "Odoo responded to the read-only check."),
                    (failed, database_message),
                    (unknown, "Waiting for database access."),
                    (unknown, "Waiting for an authenticated principal check."),
                    f"ODOO_API_HTTP_{status_code}",
                )
        elif isinstance(error, ConnectorIncompleteResultError):
            values = (
                (ready, "Odoo responded to the read-only check."),
                (failed, "Odoo returned incomplete information for this database."),
                (unknown, "Waiting for complete database access."),
                (unknown, "Waiting for a complete principal check."),
                "ODOO_RESPONSE_INCOMPLETE",
            )
        elif isinstance(error, ConnectorError):
            values = (
                (ready, "Odoo responded to the read-only check."),
                (failed, "Odoo could not complete the read-only database check."),
                (unknown, "Waiting for database access."),
                (unknown, "Waiting for an authenticated principal check."),
                "ODOO_CONNECTION_FAILED",
            )
        else:
            values = (
                (unknown, "The Odoo server check did not complete."),
                (failed, "Impodo could not complete the read-only database check."),
                (unknown, "Waiting for database access."),
                (unknown, "Waiting for an authenticated principal check."),
                "ODOO_CONNECTION_FAILED",
            )
        server, database, version, principal, support_code = values
        status = _status(
            target_hash=target_hash,
            server=server,
            database=database,
            version=version,
            principal=principal,
            checked_at=_now(),
            support_code=support_code,
        )
        with self._lock:
            self._statuses[(project.project_id, purpose)] = status
        return status


def _project_target_hash(project: MigrationProject) -> str:
    mode = (
        project.odoo_connection_mode.value
        if project.odoo_connection_mode is not None
        else ""
    )
    return target_identity_hash(
        connection_mode=mode,
        base_url=project.odoo_base_url,
        database=project.odoo_database,
    )


def _unchecked_status(target_hash: str) -> RemoteConnectionStatus:
    return _status(
        target_hash=target_hash,
        server=(RemoteConnectionLevel.UNKNOWN, "Not checked yet."),
        database=(
            RemoteConnectionLevel.UNKNOWN,
            "Waiting for the Odoo server check.",
        ),
        version=(
            RemoteConnectionLevel.UNKNOWN,
            "Waiting for database access.",
        ),
        principal=(
            RemoteConnectionLevel.UNKNOWN,
            "Waiting for an authenticated principal check.",
        ),
        checked_at=None,
    )


def _status(
    *,
    target_hash: str,
    server: tuple[RemoteConnectionLevel, str],
    database: tuple[RemoteConnectionLevel, str],
    version: tuple[RemoteConnectionLevel, str],
    principal: tuple[RemoteConnectionLevel, str],
    checked_at: str | None,
    support_code: str | None = None,
    read_principal_hash: str | None = None,
) -> RemoteConnectionStatus:
    return RemoteConnectionStatus(
        target_hash=target_hash,
        checked_at=checked_at,
        checks=(
            RemoteConnectionCheck(
                key="server",
                label="Odoo server",
                level=server[0],
                message=server[1],
                waiting_message="Not checked yet.",
            ),
            RemoteConnectionCheck(
                key="database",
                label="Database access",
                level=database[0],
                message=database[1],
                waiting_message="Waiting for the Odoo server check.",
            ),
            RemoteConnectionCheck(
                key="version",
                label="Odoo version",
                level=version[0],
                message=version[1],
                waiting_message="Waiting for database access.",
            ),
            RemoteConnectionCheck(
                key="principal",
                label="Read-only principal",
                level=principal[0],
                message=principal[1],
                waiting_message="Waiting for an authenticated principal check.",
            ),
        ),
        support_code=support_code,
        read_principal_hash=read_principal_hash,
    )


def _http_status(error: Exception) -> int | None:
    matched = re.search(r"\bHTTP\s+(\d{3})\b", str(error))
    return int(matched.group(1)) if matched is not None else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
