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

from impodo.domain.shared.models import OdooReadIdentity, TargetFingerprint, target_identity_hash
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, WorkspaceStateError


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
    def from_workspace(
        cls,
        workspace_state: WorkspaceState,
    ) -> "OdooConnectionIdentity":
        if workspace_state.odoo_connection_mode is None:
            raise WorkspaceStateError("Choose Local Odoo or Remote Odoo")
        return cls(
            connection_mode=workspace_state.odoo_connection_mode,
            base_url=workspace_state.odoo_base_url,
            database=workspace_state.odoo_database,
            identity_hash=target_identity_hash(
                connection_mode=workspace_state.odoo_connection_mode.value,
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
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
        workspace_state: WorkspaceState,
        api_key: str,
        *,
        purpose: OdooConnectionPurpose,
    ) -> OdooConnectionTestResult:
        """Test identity and authentication without model/schema discovery."""

        if purpose is OdooConnectionPurpose.TARGET_WRITE:
            raise WorkspaceStateError("Use the write-access check at load confirmation")
        connection = OdooConnectionIdentity.from_workspace(workspace_state)
        fingerprint = self._fingerprint_probe(workspace_state, api_key)
        read_identity = self._read_identity_probe(
            workspace_state,
            api_key,
            ("res.users",),
        )
        return OdooConnectionTestResult(
            purpose=purpose,
            connection=connection,
            fingerprint=fingerprint,
            read_identity=read_identity,
        )


