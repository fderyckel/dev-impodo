"""Protected, target-bound Odoo origin and manifest contracts.

Bulk captured business values stay in the immutable typed source artifact.
This module carries only narrow origin columns and small manifest metadata; it
never creates per-row signatures or exposes numeric Odoo identifiers through a
portable source contract.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import json
import re
from uuid import UUID

from .odoo_capture import (
    ODOO_CAPTURE_PAGE_SIZE,
    MAX_ODOO_CAPTURE_ROWS,
    OdooCaptureConsistency,
    OdooCaptureSelection,
)
from .odoo_source_policy import CURRENT_ODOO_SOURCE_POLICY
from .serialization import canonical_json, content_hash


ODOO_CAPTURE_MANIFEST_VERSION = 2
ODOO_EXECUTION_ORIGIN_MANIFEST_VERSION = 2
ODOO_ORIGIN_BATCH_MAX_ROWS = ODOO_CAPTURE_PAGE_SIZE

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL = re.compile(r"[a-z_][a-z0-9_.]{0,127}")
_FIELD = re.compile(r"[a-z_][a-z0-9_]{0,127}")
_STORAGE_KEY = re.compile(r"[a-z0-9][a-z0-9_./-]{0,511}")
_MAX_ODOO_ID = 2**63 - 1


class OdooProvenanceError(ValueError):
    """Raised when protected Odoo provenance is malformed or inconsistent."""


@dataclass(frozen=True, slots=True)
class OdooRelationshipOriginColumn:
    """Protected source identifiers for one governed relational field.

    These identifiers are capture-local evidence only. Stage 5 joins them to
    another captured dataset's governed business key before any portable
    relationship contract is created.
    """

    field_name: str
    kind: str
    relation_model: str
    values: tuple[tuple[int, ...], ...]

    def __post_init__(self) -> None:
        if _FIELD.fullmatch(self.field_name) is None:
            raise OdooProvenanceError("Relationship origin field is invalid")
        if self.kind not in CURRENT_ODOO_SOURCE_POLICY.capture_relationship_types:
            raise OdooProvenanceError("Relationship origin kind is invalid")
        if _MODEL.fullmatch(self.relation_model) is None:
            raise OdooProvenanceError("Relationship origin model is invalid")
        normalized: list[tuple[int, ...]] = []
        for raw_members in self.values:
            members = tuple(int(value) for value in raw_members)
            if (
                len(members)
                > CURRENT_ODOO_SOURCE_POLICY.max_relationship_members_per_row
            ):
                raise OdooProvenanceError(
                    "Relationship origin exceeds the per-row member limit"
                )
            if any(not 1 <= value <= _MAX_ODOO_ID for value in members):
                raise OdooProvenanceError("Relationship origin ID is invalid")
            if self.kind == "many2one" and len(members) > 1:
                raise OdooProvenanceError(
                    "Many2one relationship origin has multiple members"
                )
            if members != tuple(sorted(set(members))):
                raise OdooProvenanceError(
                    "Relationship origin members must be sorted and unique"
                )
            normalized.append(members)
        object.__setattr__(self, "values", tuple(normalized))


@dataclass(frozen=True, slots=True)
class OdooOriginBatch:
    """One bounded columnar page of protected capture origins.

    Ordinals are implicit and contiguous from ``first_row_ordinal``. Numeric
    identifiers and write timestamps are columns, not row objects or JSON.
    """

    first_row_ordinal: int
    odoo_ids: tuple[int, ...]
    write_dates: tuple[datetime | None, ...]
    relationships: tuple[OdooRelationshipOriginColumn, ...] = ()

    def __post_init__(self) -> None:
        ids = tuple(int(value) for value in self.odoo_ids)
        dates = tuple(self.write_dates)
        relationships = tuple(self.relationships)
        if not 1 <= self.first_row_ordinal <= MAX_ODOO_CAPTURE_ROWS:
            raise OdooProvenanceError("Origin batch ordinal must be positive")
        if not ids or len(ids) > ODOO_ORIGIN_BATCH_MAX_ROWS:
            raise OdooProvenanceError("Origin batch is empty or exceeds the page limit")
        if len(dates) != len(ids):
            raise OdooProvenanceError("Origin batch columns have different lengths")
        if (
            len(relationships) > CURRENT_ODOO_SOURCE_POLICY.max_relationship_fields
            or any(len(column.values) != len(ids) for column in relationships)
            or tuple(column.field_name for column in relationships)
            != tuple(sorted({column.field_name for column in relationships}))
        ):
            raise OdooProvenanceError(
                "Origin relationship columns are inconsistent"
            )
        if any(not 1 <= value <= _MAX_ODOO_ID for value in ids) or any(
            current <= previous for previous, current in zip(ids, ids[1:])
        ):
            raise OdooProvenanceError(
                "Odoo origin identifiers must be positive and strictly increasing"
            )
        normalized_dates: list[datetime | None] = []
        for value in dates:
            if value is None:
                normalized_dates.append(None)
                continue
            if value.tzinfo is None:
                raise OdooProvenanceError(
                    "Odoo origin write timestamps must be timezone-aware"
                )
            normalized_dates.append(value.astimezone(timezone.utc))
        object.__setattr__(self, "odoo_ids", ids)
        object.__setattr__(self, "write_dates", tuple(normalized_dates))
        object.__setattr__(self, "relationships", relationships)

    @property
    def row_count(self) -> int:
        return len(self.odoo_ids)


@dataclass(frozen=True, slots=True)
class OdooCaptureOriginHeader:
    """Small protected header encrypted with the origin columns."""

    high_water_id: int

    def __post_init__(self) -> None:
        if not 0 <= self.high_water_id <= _MAX_ODOO_ID:
            raise OdooProvenanceError("Odoo capture high-water ID is invalid")


@dataclass(frozen=True, slots=True)
class OdooExecutionOriginBatch:
    """Map existing execution-row hashes to capture ordinals in bounded columns.

    The row hashes are reused from the portable execution snapshot. This
    contract does not calculate another per-row digest or duplicate Odoo IDs.
    """

    execution_row_hashes: tuple[str, ...]
    source_row_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        hashes = tuple(self.execution_row_hashes)
        ordinals = tuple(int(value) for value in self.source_row_ordinals)
        if not hashes or len(hashes) > ODOO_ORIGIN_BATCH_MAX_ROWS:
            raise OdooProvenanceError(
                "Execution-origin batch is empty or exceeds the page limit"
            )
        if len(hashes) != len(ordinals):
            raise OdooProvenanceError(
                "Execution-origin batch columns have different lengths"
            )
        if any(_HASH.fullmatch(value) is None for value in hashes):
            raise OdooProvenanceError("Execution row hash is invalid")
        if len(set(hashes)) != len(hashes):
            raise OdooProvenanceError("Execution row hashes must be unique")
        if any(not 1 <= value <= MAX_ODOO_CAPTURE_ROWS for value in ordinals):
            raise OdooProvenanceError("Source row ordinal must be positive")
        if len(set(ordinals)) != len(ordinals):
            raise OdooProvenanceError("Source row ordinals must be unique")
        object.__setattr__(self, "execution_row_hashes", hashes)
        object.__setattr__(self, "source_row_ordinals", ordinals)

    @property
    def row_count(self) -> int:
        return len(self.execution_row_hashes)


@dataclass(frozen=True, slots=True)
class OdooProvenanceBinding:
    """Small authenticated-data binding for one encrypted sidecar."""

    manifest_id: str
    data_version_id: str
    selection_hash: str
    dataset_id: str
    model: str
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    context_hash: str

    def __post_init__(self) -> None:
        _require_uuid(self.manifest_id, "manifest ID")
        _require_uuid(self.data_version_id, "DataVersion ID")
        for value, label in (
            (self.selection_hash, "selection hash"),
            (self.connection_target_hash, "connection target hash"),
            (self.schema_scope_hash, "schema scope hash"),
            (self.read_principal_hash, "read principal hash"),
            (self.context_hash, "context hash"),
        ):
            _require_hash(value, label)
        if not self.dataset_id or len(self.dataset_id) > 128:
            raise OdooProvenanceError("Odoo provenance dataset ID is invalid")
        if _MODEL.fullmatch(self.model) is None:
            raise OdooProvenanceError("Odoo provenance model is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "connection_target_hash": self.connection_target_hash,
            "context_hash": self.context_hash,
            "dataset_id": self.dataset_id,
            "manifest_id": self.manifest_id,
            "model": self.model,
            "data_version_id": self.data_version_id,
            "read_principal_hash": self.read_principal_hash,
            "schema_scope_hash": self.schema_scope_hash,
            "selection_hash": self.selection_hash,
        }

    def authenticated_bytes(self) -> bytes:
        """Encode this bounded control-plane binding once for AEAD AAD."""

        return canonical_json(self.to_dict()).encode("utf-8")


@dataclass(frozen=True, slots=True)
class OdooCaptureManifest:
    """Immutable roots and bindings for one Odoo values/origin publication."""

    manifest_id: str
    data_version_id: str
    selection_id: str
    selection_version: int
    selection_hash: str
    policy_hash: str
    dataset_id: str
    dataset_name: str
    model: str
    field_names: tuple[str, ...]
    column_stable_keys: tuple[str, ...]
    connection_target_hash: str
    schema_scope_hash: str
    read_principal_hash: str
    read_permission_hash: str
    context_hash: str
    consistency: OdooCaptureConsistency
    row_count: int
    data_logical_hash: str
    data_sha256: str
    data_storage_key: str
    data_size_bytes: int
    provenance_logical_hash: str
    provenance_sha256: str
    provenance_storage_key: str
    provenance_size_bytes: int
    capture_started_at: datetime
    capture_finished_at: datetime
    retention_until: datetime
    created_by: str
    content_hash: str
    contract_version: int = ODOO_CAPTURE_MANIFEST_VERSION
    _calculate_content_hash: InitVar[bool] = False

    def __post_init__(self, _calculate_content_hash: bool) -> None:
        if self.contract_version != ODOO_CAPTURE_MANIFEST_VERSION:
            raise OdooProvenanceError("Unsupported Odoo capture manifest version")
        for value, label in (
            (self.manifest_id, "manifest ID"),
            (self.data_version_id, "DataVersion ID"),
            (self.selection_id, "selection ID"),
        ):
            _require_uuid(value, label)
        if self.selection_version < 1:
            raise OdooProvenanceError("Odoo selection version must be positive")
        for value, label in (
            (self.selection_hash, "selection hash"),
            (self.policy_hash, "policy hash"),
            (self.connection_target_hash, "connection target hash"),
            (self.schema_scope_hash, "schema scope hash"),
            (self.read_principal_hash, "read principal hash"),
            (self.read_permission_hash, "read permission hash"),
            (self.context_hash, "context hash"),
            (self.data_logical_hash, "data logical hash"),
            (self.data_sha256, "data artifact hash"),
            (self.provenance_logical_hash, "provenance logical hash"),
            (self.provenance_sha256, "provenance artifact hash"),
        ):
            _require_hash(value, label)
        if not self.dataset_id or len(self.dataset_id) > 128:
            raise OdooProvenanceError("Odoo manifest dataset ID is invalid")
        if not self.dataset_name or len(self.dataset_name) > 128:
            raise OdooProvenanceError("Odoo manifest dataset name is invalid")
        if _MODEL.fullmatch(self.model) is None:
            raise OdooProvenanceError("Odoo manifest model is invalid")
        if (
            not self.field_names
            or len(self.field_names) != len(self.column_stable_keys)
            or self.field_names != tuple(sorted(set(self.field_names)))
            or len(set(self.column_stable_keys)) != len(self.column_stable_keys)
        ):
            raise OdooProvenanceError("Odoo manifest field index is invalid")
        if not 0 <= self.row_count <= MAX_ODOO_CAPTURE_ROWS:
            raise OdooProvenanceError("Odoo manifest row count is invalid")
        for key in (self.data_storage_key, self.provenance_storage_key):
            _require_storage_key(key)
        if (
            not 0 <= self.data_size_bytes
            <= CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes
            or not 1 <= self.provenance_size_bytes
            <= CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes
        ):
            raise OdooProvenanceError("Odoo manifest artifact size is invalid")
        for value, label in (
            (self.capture_started_at, "capture start"),
            (self.capture_finished_at, "capture finish"),
            (self.retention_until, "retention deadline"),
        ):
            if value.tzinfo is None:
                raise OdooProvenanceError(f"{label} must be timezone-aware")
        if not (
            self.capture_started_at <= self.capture_finished_at
            < self.retention_until
        ):
            raise OdooProvenanceError("Odoo capture time or retention range is invalid")
        if not self.created_by.strip() or len(self.created_by) > 500:
            raise OdooProvenanceError("Odoo manifest actor is invalid")
        expected_hash = content_hash(self._semantic_dict())
        if _calculate_content_hash:
            if self.content_hash:
                raise OdooProvenanceError("New Odoo manifest already has a hash")
            object.__setattr__(self, "content_hash", expected_hash)
        else:
            _require_hash(self.content_hash, "manifest content hash")
        if self.content_hash != expected_hash:
            raise OdooProvenanceError("Odoo capture manifest hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        manifest_id: str,
        selection: OdooCaptureSelection,
        dataset_id: str,
        column_stable_keys: tuple[str, ...],
        row_count: int,
        data_logical_hash: str,
        data_sha256: str,
        data_storage_key: str,
        data_size_bytes: int,
        provenance_logical_hash: str,
        provenance_sha256: str,
        provenance_storage_key: str,
        provenance_size_bytes: int,
        capture_started_at: datetime,
        capture_finished_at: datetime,
        retention_until: datetime,
        created_by: str,
    ) -> OdooCaptureManifest:
        return cls(
            manifest_id=manifest_id,
            data_version_id=selection.data_version_id,
            selection_id=selection.selection_id,
            selection_version=selection.version,
            selection_hash=selection.content_hash,
            policy_hash=selection.policy_hash,
            dataset_id=dataset_id,
            dataset_name=selection.dataset_name,
            model=selection.model,
            field_names=selection.field_names,
            column_stable_keys=column_stable_keys,
            connection_target_hash=selection.connection_target_hash,
            schema_scope_hash=selection.schema_scope_hash,
            read_principal_hash=selection.read_principal_hash,
            read_permission_hash=selection.read_permission_hash,
            context_hash=selection.context_hash,
            consistency=selection.consistency,
            row_count=row_count,
            data_logical_hash=data_logical_hash,
            data_sha256=data_sha256,
            data_storage_key=data_storage_key,
            data_size_bytes=data_size_bytes,
            provenance_logical_hash=provenance_logical_hash,
            provenance_sha256=provenance_sha256,
            provenance_storage_key=provenance_storage_key,
            provenance_size_bytes=provenance_size_bytes,
            capture_started_at=capture_started_at,
            capture_finished_at=capture_finished_at,
            retention_until=retention_until,
            created_by=created_by,
            content_hash="",
            _calculate_content_hash=True,
        )

    @property
    def provenance_binding(self) -> OdooProvenanceBinding:
        return OdooProvenanceBinding(
            manifest_id=self.manifest_id,
            data_version_id=self.data_version_id,
            selection_hash=self.selection_hash,
            dataset_id=self.dataset_id,
            model=self.model,
            connection_target_hash=self.connection_target_hash,
            schema_scope_hash=self.schema_scope_hash,
            read_principal_hash=self.read_principal_hash,
            context_hash=self.context_hash,
        )

    def binds_selection(self, selection: OdooCaptureSelection) -> bool:
        """Verify the bounded manifest index against one selection once."""

        return (
            self.data_version_id == selection.data_version_id
            and self.selection_id == selection.selection_id
            and self.selection_version == selection.version
            and self.selection_hash == selection.content_hash
            and self.policy_hash == selection.policy_hash
            and self.dataset_name == selection.dataset_name
            and self.model == selection.model
            and self.field_names == selection.field_names
            and self.connection_target_hash == selection.connection_target_hash
            and self.schema_scope_hash == selection.schema_scope_hash
            and self.read_principal_hash == selection.read_principal_hash
            and self.read_permission_hash == selection.read_permission_hash
            and self.context_hash == selection.context_hash
            and self.consistency == selection.consistency
            and self.row_count <= selection.max_rows
        )

    def _semantic_dict(self) -> dict[str, object]:
        return {
            "capture_finished_at": self.capture_finished_at.isoformat(),
            "capture_started_at": self.capture_started_at.isoformat(),
            "column_stable_keys": list(self.column_stable_keys),
            "connection_target_hash": self.connection_target_hash,
            "consistency": self.consistency.value,
            "context_hash": self.context_hash,
            "contract_version": self.contract_version,
            "data_logical_hash": self.data_logical_hash,
            "data_sha256": self.data_sha256,
            "data_size_bytes": self.data_size_bytes,
            "data_storage_key": self.data_storage_key,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "field_names": list(self.field_names),
            "manifest_id": self.manifest_id,
            "model": self.model,
            "policy_hash": self.policy_hash,
            "data_version_id": self.data_version_id,
            "provenance_logical_hash": self.provenance_logical_hash,
            "provenance_sha256": self.provenance_sha256,
            "provenance_size_bytes": self.provenance_size_bytes,
            "provenance_storage_key": self.provenance_storage_key,
            "read_permission_hash": self.read_permission_hash,
            "read_principal_hash": self.read_principal_hash,
            "retention_until": self.retention_until.isoformat(),
            "row_count": self.row_count,
            "schema_scope_hash": self.schema_scope_hash,
            "selection_hash": self.selection_hash,
            "selection_id": self.selection_id,
            "selection_version": self.selection_version,
        }

    def to_json(self) -> str:
        return canonical_json(
            {
                **self._semantic_dict(),
                "content_hash": self.content_hash,
                "created_by": self.created_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> OdooCaptureManifest:
        try:
            payload = json.loads(value)
            _require_exact_keys(
                payload,
                set(cls._json_keys()),
            )
            return cls(
                manifest_id=str(payload["manifest_id"]),
                data_version_id=str(payload["data_version_id"]),
                selection_id=str(payload["selection_id"]),
                selection_version=int(payload["selection_version"]),
                selection_hash=str(payload["selection_hash"]),
                policy_hash=str(payload["policy_hash"]),
                dataset_id=str(payload["dataset_id"]),
                dataset_name=str(payload["dataset_name"]),
                model=str(payload["model"]),
                field_names=tuple(str(item) for item in payload["field_names"]),
                column_stable_keys=tuple(
                    str(item) for item in payload["column_stable_keys"]
                ),
                connection_target_hash=str(payload["connection_target_hash"]),
                schema_scope_hash=str(payload["schema_scope_hash"]),
                read_principal_hash=str(payload["read_principal_hash"]),
                read_permission_hash=str(payload["read_permission_hash"]),
                context_hash=str(payload["context_hash"]),
                consistency=OdooCaptureConsistency(payload["consistency"]),
                row_count=int(payload["row_count"]),
                data_logical_hash=str(payload["data_logical_hash"]),
                data_sha256=str(payload["data_sha256"]),
                data_storage_key=str(payload["data_storage_key"]),
                data_size_bytes=int(payload["data_size_bytes"]),
                provenance_logical_hash=str(payload["provenance_logical_hash"]),
                provenance_sha256=str(payload["provenance_sha256"]),
                provenance_storage_key=str(payload["provenance_storage_key"]),
                provenance_size_bytes=int(payload["provenance_size_bytes"]),
                capture_started_at=datetime.fromisoformat(
                    str(payload["capture_started_at"])
                ),
                capture_finished_at=datetime.fromisoformat(
                    str(payload["capture_finished_at"])
                ),
                retention_until=datetime.fromisoformat(
                    str(payload["retention_until"])
                ),
                created_by=str(payload["created_by"]),
                content_hash=str(payload["content_hash"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, OdooProvenanceError):
                raise
            raise OdooProvenanceError("Odoo capture manifest is invalid") from error

    @staticmethod
    def _json_keys() -> tuple[str, ...]:
        return (
            "capture_finished_at", "capture_started_at", "column_stable_keys",
            "connection_target_hash", "consistency", "content_hash",
            "context_hash", "contract_version", "created_by",
            "data_logical_hash", "data_sha256", "data_size_bytes",
            "data_storage_key", "dataset_id", "dataset_name", "field_names",
            "manifest_id", "model", "policy_hash", "data_version_id",
            "provenance_logical_hash", "provenance_sha256",
            "provenance_size_bytes", "provenance_storage_key",
            "read_permission_hash", "read_principal_hash", "retention_until",
            "row_count", "schema_scope_hash", "selection_hash", "selection_id",
            "selection_version",
        )


@dataclass(frozen=True, slots=True)
class OdooExecutionOriginManifest:
    """Protected companion binding execution rows to captured origin ordinals.

    The companion reuses execution row hashes that already exist in the
    portable snapshot. It adds no per-row hash and contains no business values.
    """

    origin_id: str
    workspace_id: str
    capture_manifest_hash: str
    execution_snapshot_hash: str
    connection_target_hash: str
    write_principal_hash: str
    write_permission_hash: str
    context_hash: str
    row_count: int
    logical_hash: str
    artifact_sha256: str
    storage_key: str
    size_bytes: int
    created_at: datetime
    created_by: str
    content_hash: str
    contract_version: int = ODOO_EXECUTION_ORIGIN_MANIFEST_VERSION
    _calculate_content_hash: InitVar[bool] = False

    def __post_init__(self, _calculate_content_hash: bool) -> None:
        if self.contract_version != ODOO_EXECUTION_ORIGIN_MANIFEST_VERSION:
            raise OdooProvenanceError(
                "Unsupported Odoo execution-origin manifest version"
            )
        _require_uuid(self.origin_id, "execution-origin ID")
        _require_uuid(self.workspace_id, "workspace ID")
        for value, label in (
            (self.capture_manifest_hash, "capture manifest hash"),
            (self.execution_snapshot_hash, "execution snapshot hash"),
            (self.connection_target_hash, "connection target hash"),
            (self.write_principal_hash, "write principal hash"),
            (self.write_permission_hash, "write permission hash"),
            (self.context_hash, "context hash"),
            (self.logical_hash, "execution-origin logical hash"),
            (self.artifact_sha256, "execution-origin artifact hash"),
        ):
            _require_hash(value, label)
        if (
            not 0 <= self.row_count <= MAX_ODOO_CAPTURE_ROWS
            or not 1 <= self.size_bytes
            <= CURRENT_ODOO_SOURCE_POLICY.max_snapshot_bytes
        ):
            raise OdooProvenanceError("Odoo execution-origin size is invalid")
        _require_storage_key(self.storage_key)
        if self.created_at.tzinfo is None:
            raise OdooProvenanceError(
                "Odoo execution-origin creation time must be timezone-aware"
            )
        if not self.created_by.strip() or len(self.created_by) > 500:
            raise OdooProvenanceError("Odoo execution-origin actor is invalid")
        expected_hash = content_hash(self._semantic_dict())
        if _calculate_content_hash:
            if self.content_hash:
                raise OdooProvenanceError(
                    "New Odoo execution-origin manifest already has a hash"
                )
            object.__setattr__(self, "content_hash", expected_hash)
        else:
            _require_hash(self.content_hash, "execution-origin content hash")
        if self.content_hash != expected_hash:
            raise OdooProvenanceError(
                "Odoo execution-origin manifest hash is invalid"
            )

    @classmethod
    def create(
        cls,
        *,
        origin_id: str,
        workspace_id: str,
        capture_manifest_hash: str,
        execution_snapshot_hash: str,
        connection_target_hash: str,
        write_principal_hash: str,
        write_permission_hash: str,
        context_hash: str,
        row_count: int,
        logical_hash: str,
        artifact_sha256: str,
        storage_key: str,
        size_bytes: int,
        created_at: datetime,
        created_by: str,
    ) -> OdooExecutionOriginManifest:
        """Create the exact current manifest and calculate its hash once."""

        return cls(
            origin_id=origin_id,
            workspace_id=workspace_id,
            capture_manifest_hash=capture_manifest_hash,
            execution_snapshot_hash=execution_snapshot_hash,
            connection_target_hash=connection_target_hash,
            write_principal_hash=write_principal_hash,
            write_permission_hash=write_permission_hash,
            context_hash=context_hash,
            row_count=row_count,
            logical_hash=logical_hash,
            artifact_sha256=artifact_sha256,
            storage_key=storage_key,
            size_bytes=size_bytes,
            created_at=created_at,
            created_by=created_by,
            content_hash="",
            _calculate_content_hash=True,
        )

    def _semantic_dict(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "capture_manifest_hash": self.capture_manifest_hash,
            "connection_target_hash": self.connection_target_hash,
            "context_hash": self.context_hash,
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "execution_snapshot_hash": self.execution_snapshot_hash,
            "logical_hash": self.logical_hash,
            "origin_id": self.origin_id,
            "workspace_id": self.workspace_id,
            "row_count": self.row_count,
            "size_bytes": self.size_bytes,
            "storage_key": self.storage_key,
            "write_permission_hash": self.write_permission_hash,
            "write_principal_hash": self.write_principal_hash,
        }

    def to_json(self) -> str:
        return canonical_json(
            {
                **self._semantic_dict(),
                "content_hash": self.content_hash,
                "created_by": self.created_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> OdooExecutionOriginManifest:
        try:
            payload = json.loads(value)
            expected = {
                "artifact_sha256", "capture_manifest_hash",
                "connection_target_hash", "content_hash", "context_hash",
                "contract_version", "created_at", "created_by",
                "execution_snapshot_hash", "logical_hash", "origin_id",
                "workspace_id", "row_count", "size_bytes", "storage_key",
                "write_permission_hash", "write_principal_hash",
            }
            _require_exact_keys(payload, expected)
            return cls(
                origin_id=str(payload["origin_id"]),
                workspace_id=str(payload["workspace_id"]),
                capture_manifest_hash=str(payload["capture_manifest_hash"]),
                execution_snapshot_hash=str(payload["execution_snapshot_hash"]),
                connection_target_hash=str(payload["connection_target_hash"]),
                write_principal_hash=str(payload["write_principal_hash"]),
                write_permission_hash=str(payload["write_permission_hash"]),
                context_hash=str(payload["context_hash"]),
                row_count=int(payload["row_count"]),
                logical_hash=str(payload["logical_hash"]),
                artifact_sha256=str(payload["artifact_sha256"]),
                storage_key=str(payload["storage_key"]),
                size_bytes=int(payload["size_bytes"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
                created_by=str(payload["created_by"]),
                content_hash=str(payload["content_hash"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, OdooProvenanceError):
                raise
            raise OdooProvenanceError(
                "Odoo execution-origin manifest is invalid"
            ) from error


def _require_hash(value: str, label: str) -> None:
    if _HASH.fullmatch(value) is None:
        raise OdooProvenanceError(f"Odoo {label} is invalid")


def _require_uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (AttributeError, ValueError) as error:
        raise OdooProvenanceError(f"Odoo {label} is invalid") from error


def _require_storage_key(value: str) -> None:
    if (
        _STORAGE_KEY.fullmatch(value) is None
        or value.startswith("/")
        or ".." in value.split("/")
    ):
        raise OdooProvenanceError("Odoo artifact storage key is invalid")


def _require_exact_keys(payload: object, expected: set[str]) -> None:
    if not isinstance(payload, dict) or set(payload) != expected:
        raise OdooProvenanceError("Odoo protected contract shape is invalid")
