"""Test one exact Odoo connection without discovering business metadata.

Connection testing is shared by source setup, destination setup, Recipe Test,
Production, and reconnect flows.  It identifies the endpoint, database,
supported Odoo version, and authenticated read principal only.  Model catalogue
and ``fields_get`` discovery remain explicit later-stage operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from ..models import OdooReadIdentity, TargetFingerprint, target_identity_hash
from ..projects import WorkspaceState, OdooConnectionMode, ProjectError


class OdooConnectionPurpose(StrEnum):
    """Name the capability for which one connection is being checked."""

    SOURCE_READ = "SOURCE_READ"
    TARGET_READ = "TARGET_READ"
    TARGET_WRITE = "TARGET_WRITE"


@dataclass(frozen=True, slots=True)
class OdooConnectionIdentity:
    """Non-secret identity of one exact Odoo database connection."""

    connection_mode: OdooConnectionMode
    base_url: str
    database: str
    identity_hash: str

    @classmethod
    def from_project(cls, project: WorkspaceState) -> "OdooConnectionIdentity":
        if project.odoo_connection_mode is None:
            raise ProjectError("Choose Local Odoo or Remote Odoo")
        return cls(
            connection_mode=project.odoo_connection_mode,
            base_url=project.odoo_base_url,
            database=project.odoo_database,
            identity_hash=target_identity_hash(
                connection_mode=project.odoo_connection_mode.value,
                base_url=project.odoo_base_url,
                database=project.odoo_database,
            ),
        )


@dataclass(frozen=True, slots=True)
class OdooConnectionTestResult:
    """Session-scoped result of one bounded, read-only Odoo probe."""

    purpose: OdooConnectionPurpose
    connection: OdooConnectionIdentity
    fingerprint: TargetFingerprint
    read_identity: OdooReadIdentity


ConnectionFingerprintProbe = Callable[[WorkspaceState, str], TargetFingerprint]
ConnectionReadIdentityProbe = Callable[
    [WorkspaceState, str, tuple[str, ...]],
    OdooReadIdentity,
]


class OdooConnectionTestService:
    """Coordinate the shared remote/JSON-2 connection test contract."""

    def __init__(
        self,
        fingerprint_probe: ConnectionFingerprintProbe,
        read_identity_probe: ConnectionReadIdentityProbe,
    ) -> None:
        self._fingerprint_probe = fingerprint_probe
        self._read_identity_probe = read_identity_probe

    def test_read(
        self,
        project: WorkspaceState,
        api_key: str,
        *,
        purpose: OdooConnectionPurpose,
    ) -> OdooConnectionTestResult:
        """Test identity and authentication without model/schema discovery."""

        if purpose is OdooConnectionPurpose.TARGET_WRITE:
            raise ProjectError("Use the write-access check at load confirmation")
        connection = OdooConnectionIdentity.from_project(project)
        fingerprint = self._fingerprint_probe(project, api_key)
        read_identity = self._read_identity_probe(
            project,
            api_key,
            ("res.users",),
        )
        return OdooConnectionTestResult(
            purpose=purpose,
            connection=connection,
            fingerprint=fingerprint,
            read_identity=read_identity,
        )

