"""Per-major current policy contracts for governed Odoo-source capture.

The policy is deliberately executable evidence rather than release prose.  A
capture selection binds its hash, so changing identity, limits, field scope,
or protected-data handling invalidates the current selection instead of
silently widening it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum

from .serialization import content_hash


ODOO_SOURCE_POLICY_CONTRACT_VERSION = 4


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
    capture_relationship_types: tuple[str, ...]
    writable_field_types: tuple[str, ...]
    max_fields: int
    max_relationship_fields: int
    max_relationship_members_per_row: int
    max_rows: int
    page_size: int
    max_filter_clauses: int
    max_filter_set_members: int
    max_filter_bytes: int
    max_sample_rows: int
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

ODOO_19_SOURCE_POLICY = OdooSourcePolicy(
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
        "float",
        "integer",
        "selection",
        "text",
    ),
    capture_relationship_types=("many2one", "many2many", "one2many"),
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
    max_relationship_fields=50,
    max_relationship_members_per_row=10_000,
    max_rows=10_000,
    page_size=500,
    max_filter_clauses=8,
    max_filter_set_members=100,
    max_filter_bytes=16 * 1024,
    max_sample_rows=50,
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

# Final Odoo 20 uses the same bounded read shape after Phase 3 qualification.
# Its separate object and hash prevent a later policy change for one major
# from silently changing evidence created for the other.
ODOO_20_SOURCE_POLICY = replace(
    ODOO_19_SOURCE_POLICY,
    odoo_major_version=20,
)
ODOO_SOURCE_POLICIES = {
    19: ODOO_19_SOURCE_POLICY,
    20: ODOO_20_SOURCE_POLICY,
}

# Compatibility aliases keep existing Odoo 19 artifacts and callers stable.
CURRENT_ODOO_SOURCE_POLICY = ODOO_19_SOURCE_POLICY

# The policy is immutable process metadata. Canonicalize and hash it exactly
# once, then reuse this fixed boundary value in every catalog and manifest.
ODOO_SOURCE_POLICY_HASHES = {
    major: content_hash(policy.to_dict())
    for major, policy in ODOO_SOURCE_POLICIES.items()
}
ODOO_SOURCE_POLICY_HASH = ODOO_SOURCE_POLICY_HASHES[19]


def odoo_source_policy(odoo_major_version: int) -> OdooSourcePolicy | None:
    """Return the reviewed source-capture policy for one Odoo major."""

    return ODOO_SOURCE_POLICIES.get(odoo_major_version)


def odoo_source_policy_hash(odoo_major_version: int) -> str | None:
    """Return the distinct current policy identity for one Odoo major."""

    return ODOO_SOURCE_POLICY_HASHES.get(odoo_major_version)


def odoo_source_policy_from_hash(policy_hash: str) -> OdooSourcePolicy | None:
    """Resolve only a current policy hash; historical hashes stay read-only."""

    return next(
        (
            ODOO_SOURCE_POLICIES[major]
            for major, candidate in ODOO_SOURCE_POLICY_HASHES.items()
            if candidate == policy_hash
        ),
        None,
    )

# Earlier selections remain readable as historical evidence. Planning a new
# capture still requires ODOO_SOURCE_POLICY_HASH and an updated selection.
PREVIOUS_ODOO_SOURCE_POLICY_HASH = (
    "sha256:403be4a671a9e2a25ddee994ff0c337e0b271438a7ae47a60d68f5be3572ac01"
)
READABLE_ODOO_SOURCE_POLICY_HASHES = frozenset(
    {*ODOO_SOURCE_POLICY_HASHES.values(), PREVIOUS_ODOO_SOURCE_POLICY_HASH}
)
