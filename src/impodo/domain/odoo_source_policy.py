"""One current policy contract for governed Odoo-source capture.

The policy is deliberately executable evidence rather than release prose.  A
capture selection binds its hash, so changing identity, limits, field scope,
or protected-data handling invalidates the current selection instead of
silently widening it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from .serialization import content_hash


ODOO_SOURCE_POLICY_CONTRACT_VERSION = 1


class TargetInstanceAssurance(StrEnum):
    """Strength of target identity available through the current connector."""

    CONNECTION_ONLY = "CONNECTION_ONLY"


class ProductionWriteDisposition(StrEnum):
    """Current production-write feasibility result for Odoo-source rows."""

    PRODUCTION_WRITE_UNSUPPORTED = "PRODUCTION_WRITE_UNSUPPORTED"


class ProtectedEvidenceEncryption(StrEnum):
    """At-rest decision for target-bound provenance and difference evidence."""

    APPLICATION_LEVEL_REQUIRED = "APPLICATION_LEVEL_REQUIRED"


@dataclass(frozen=True, slots=True)
class OdooSourcePolicy:
    """Immutable release policy bound into every Odoo capture selection."""

    contract_version: int
    odoo_major_version: int
    api: str
    source_target_rule: str
    round_trip_rule: str
    capture_field_types: tuple[str, ...]
    writable_field_types: tuple[str, ...]
    max_fields: int
    max_rows: int
    page_size: int
    max_request_bytes: int
    max_response_bytes: int
    max_value_bytes: int
    max_row_bytes: int
    max_snapshot_bytes: int
    max_temporary_bytes: int
    max_project_history_bytes: int
    target_instance_assurance: TargetInstanceAssurance
    production_write_disposition: ProductionWriteDisposition
    protected_evidence_class: str
    protected_evidence_encryption: ProtectedEvidenceEncryption
    backup_rule: str
    deletion_rule: str

    def to_dict(self) -> dict[str, object]:
        """Return the canonical semantic policy payload."""

        return asdict(self)

CURRENT_ODOO_SOURCE_POLICY = OdooSourcePolicy(
    contract_version=ODOO_SOURCE_POLICY_CONTRACT_VERSION,
    odoo_major_version=19,
    api="JSON-2",
    source_target_rule="SAME_CONFIGURED_TARGET",
    round_trip_rule="PROTECTED_ID_UPDATE_ONLY",
    capture_field_types=(
        "boolean",
        "char",
        "date",
        "datetime",
        "integer",
        "selection",
        "text",
    ),
    writable_field_types=(
        "boolean",
        "char",
        "date",
        "datetime",
        "integer",
        "selection",
        "text",
    ),
    max_fields=50,
    max_rows=10_000,
    page_size=500,
    max_request_bytes=64 * 1024,
    max_response_bytes=8 * 1024 * 1024,
    max_value_bytes=256 * 1024,
    max_row_bytes=1024 * 1024,
    max_snapshot_bytes=256 * 1024 * 1024,
    max_temporary_bytes=512 * 1024 * 1024,
    max_project_history_bytes=2 * 1024 * 1024 * 1024,
    target_instance_assurance=TargetInstanceAssurance.CONNECTION_ONLY,
    production_write_disposition=(
        ProductionWriteDisposition.PRODUCTION_WRITE_UNSUPPORTED
    ),
    protected_evidence_class="RESTRICTED_TARGET_EVIDENCE",
    protected_evidence_encryption=(
        ProtectedEvidenceEncryption.APPLICATION_LEVEL_REQUIRED
    ),
    backup_rule="EXCLUDED_UNLESS_EXPLICITLY_APPROVED",
    deletion_rule="RETENTION_EXPIRY_OR_PROJECT_DELETION",
)

# The policy is immutable process metadata. Canonicalize and hash it exactly
# once, then reuse this fixed boundary value in every catalog and manifest.
ODOO_SOURCE_POLICY_HASH = content_hash(CURRENT_ODOO_SOURCE_POLICY.to_dict())
