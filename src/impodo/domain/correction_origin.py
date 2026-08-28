"""Protected completed-load origin evidence for governed corrections.

The contracts in this module bind existing immutable evidence without copying
prepared values into a second baseline.  The target index is hashed once as a
whole artifact; individual rows and values deliberately receive no new hash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json

from impodo.domain.project.foundation import (
    require_aware,
    require_hash,
    require_uuid,
    required_text,
)
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import ActorIdentity


CORRECTION_TARGET_INDEX_CONTRACT = "correction-target-index-v1"
CORRECTION_ORIGIN_CONTRACT = "correction-origin-v1"


class CorrectionOriginError(ValueError):
    """Reject incomplete, ambiguous, or altered correction-origin evidence."""


@dataclass(frozen=True, slots=True)
class CorrectionTargetIndexEntry:
    """Protected exact target for one completed-load source row."""

    dataset: str
    source_row: int
    row_id: str
    target_model: str
    odoo_id: int
    completed_disposition: str
    target_binding_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.dataset, "dataset"),
            (self.row_id, "row_id"),
            (self.target_model, "target_model"),
        ):
            try:
                required_text(value, name, maximum=300)
            except ValueError as error:
                raise CorrectionOriginError(
                    "Correction target index entry is invalid"
                ) from error
        if (
            self.source_row < 1
            or type(self.odoo_id) is not int
            or self.odoo_id <= 0
            or self.completed_disposition not in {"CREATE", "UPDATE", "UNCHANGED"}
        ):
            raise CorrectionOriginError("Correction target index entry is invalid")
        if self.target_binding_hash:
            _hash(self.target_binding_hash, "target_binding_hash")

    @property
    def lineage_key(self) -> tuple[str, int, str]:
        return (self.dataset, self.source_row, self.target_model)

    @property
    def exact_target_key(self) -> tuple[str, int]:
        return (self.target_model, self.odoo_id)

    def protected_dict(self) -> dict[str, object]:
        return {
            "completed_disposition": self.completed_disposition,
            "dataset": self.dataset,
            "odoo_id": self.odoo_id,
            "row_id": self.row_id,
            "source_row": self.source_row,
            "target_binding_hash": self.target_binding_hash,
            "target_model": self.target_model,
        }

    @classmethod
    def from_protected_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "CorrectionTargetIndexEntry":
        return cls(
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            row_id=str(payload["row_id"]),
            target_model=str(payload["target_model"]),
            odoo_id=int(payload["odoo_id"]),
            completed_disposition=str(payload["completed_disposition"]),
            target_binding_hash=str(payload["target_binding_hash"]),
        )


@dataclass(frozen=True, slots=True)
class CorrectionTargetIndex:
    """One deterministic, compact exact-target index for a completed load."""

    index_id: str
    project_id: str
    completed_migration_run_id: str
    completed_workspace_id: str
    entries: tuple[CorrectionTargetIndexEntry, ...]
    created_at: datetime
    index_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.index_id, "index_id"),
            (self.project_id, "project_id"),
            (self.completed_migration_run_id, "completed_migration_run_id"),
            (self.completed_workspace_id, "completed_workspace_id"),
        ):
            _uuid(value, name)
        _hash(self.index_hash, "index_hash")
        require_aware(self.created_at, "created_at")
        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))
        ordered = tuple(sorted(self.entries, key=lambda item: item.lineage_key))
        if not ordered or ordered != self.entries:
            raise CorrectionOriginError("Correction target index order is invalid")
        if len({item.lineage_key for item in ordered}) != len(ordered):
            raise CorrectionOriginError("Correction target index lineage is ambiguous")
        if len({item.exact_target_key for item in ordered}) != len(ordered):
            raise CorrectionOriginError("Correction target index targets are ambiguous")

    @classmethod
    def create(
        cls,
        *,
        index_id: str,
        project_id: str,
        completed_migration_run_id: str,
        completed_workspace_id: str,
        entries: Iterable[CorrectionTargetIndexEntry],
        created_at: datetime,
    ) -> "CorrectionTargetIndex":
        ordered = tuple(sorted(entries, key=lambda item: item.lineage_key))
        unhashed = cls(
            index_id=index_id,
            project_id=project_id,
            completed_migration_run_id=completed_migration_run_id,
            completed_workspace_id=completed_workspace_id,
            entries=ordered,
            created_at=created_at,
            index_hash="sha256:" + "0" * 64,
        )
        return replace(unhashed, index_hash=content_hash(unhashed._meaning_dict()))

    @property
    def row_count(self) -> int:
        return len(self.entries)

    def protected_json(self) -> bytes:
        return canonical_json(
            {
                "contract": CORRECTION_TARGET_INDEX_CONTRACT,
                **self._meaning_dict(),
                "index_hash": self.index_hash,
            }
        ).encode("utf-8")

    @classmethod
    def from_protected_json(cls, payload: bytes) -> "CorrectionTargetIndex":
        try:
            raw = json.loads(payload)
            if (
                not isinstance(raw, dict)
                or raw.get("contract") != CORRECTION_TARGET_INDEX_CONTRACT
            ):
                raise CorrectionOriginError(
                    "Correction target index contract is unsupported"
                )
            result = cls(
                index_id=str(raw["index_id"]),
                project_id=str(raw["project_id"]),
                completed_migration_run_id=str(raw["completed_migration_run_id"]),
                completed_workspace_id=str(raw["completed_workspace_id"]),
                entries=tuple(
                    CorrectionTargetIndexEntry.from_protected_dict(item)
                    for item in raw["entries"]
                ),
                created_at=datetime.fromisoformat(str(raw["created_at"])),
                index_hash=str(raw["index_hash"]),
            )
        except CorrectionOriginError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CorrectionOriginError(
                "Correction target index payload is invalid"
            ) from error
        if content_hash(result._meaning_dict()) != result.index_hash:
            raise CorrectionOriginError("Correction target index hash changed")
        return result

    def _meaning_dict(self) -> dict[str, object]:
        return {
            "completed_migration_run_id": self.completed_migration_run_id,
            "completed_workspace_id": self.completed_workspace_id,
            "created_at": self.created_at.isoformat(),
            "entries": [item.protected_dict() for item in self.entries],
            "index_id": self.index_id,
            "project_id": self.project_id,
        }


@dataclass(frozen=True, slots=True)
class CorrectionPreparedArtifact:
    """Reference one existing prepared Parquet artifact without copying values."""

    dataset_id: str
    dataset_name: str
    source_snapshot_hash: str
    logical_hash: str
    content_hash: str
    parquet_storage_key: str
    parquet_sha256: str
    row_count: int

    def __post_init__(self) -> None:
        for value, name in (
            (self.dataset_id, "dataset_id"),
            (self.dataset_name, "dataset_name"),
            (self.parquet_storage_key, "parquet_storage_key"),
        ):
            try:
                required_text(value, name, maximum=500)
            except ValueError as error:
                raise CorrectionOriginError(
                    "Correction prepared artifact is invalid"
                ) from error
        for value, name in (
            (self.source_snapshot_hash, "source_snapshot_hash"),
            (self.logical_hash, "logical_hash"),
            (self.content_hash, "content_hash"),
            (self.parquet_sha256, "parquet_sha256"),
        ):
            _hash(value, name)
        if self.row_count < 0:
            raise CorrectionOriginError("Correction prepared row count is invalid")

    def portable_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "logical_hash": self.logical_hash,
            "parquet_sha256": self.parquet_sha256,
            "parquet_storage_key": self.parquet_storage_key,
            "row_count": self.row_count,
            "source_snapshot_hash": self.source_snapshot_hash,
        }


@dataclass(frozen=True, slots=True)
class ProtectedCorrectionArtifactReference:
    """Registry-safe reference to one authenticated protected artifact."""

    artifact_id: str
    logical_hash: str
    storage_key: str
    artifact_hash: str

    def __post_init__(self) -> None:
        _uuid(self.artifact_id, "artifact_id")
        _hash(self.logical_hash, "logical_hash")
        _hash(self.artifact_hash, "artifact_hash")
        try:
            required_text(self.storage_key, "storage_key", maximum=1000)
        except ValueError as error:
            raise CorrectionOriginError(
                "Protected correction artifact reference is invalid"
            ) from error

    def portable_dict(self) -> dict[str, str]:
        return {
            "artifact_hash": self.artifact_hash,
            "artifact_id": self.artifact_id,
            "logical_hash": self.logical_hash,
            "storage_key": self.storage_key,
        }


@dataclass(frozen=True, slots=True)
class CorrectionOriginManifest:
    """Lean immutable binding to completed-load evidence and exact targets."""

    manifest_id: str
    project_id: str
    data_version_id: str
    completed_migration_run_id: str
    completed_workspace_id: str
    mapping_id: str
    mapping_version: int
    mapping_content_hash: str
    prepared_artifacts: tuple[CorrectionPreparedArtifact, ...]
    execution_snapshot_hash: str
    execution_snapshot_root_hash: str
    preflight_run_id: str
    execution_run_id: str
    execution_evidence_hash: str
    reconciliation_id: str
    reconciliation_hash: str
    target_hash: str
    schema_hash: str
    read_context_hash: str
    target_observed_at: str
    target_index: ProtectedCorrectionArtifactReference
    created_by: ActorIdentity
    created_at: datetime
    manifest_hash: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.manifest_id, "manifest_id"),
            (self.project_id, "project_id"),
            (self.data_version_id, "data_version_id"),
            (self.completed_migration_run_id, "completed_migration_run_id"),
            (self.completed_workspace_id, "completed_workspace_id"),
            (self.mapping_id, "mapping_id"),
            (self.preflight_run_id, "preflight_run_id"),
            (self.execution_run_id, "execution_run_id"),
            (self.reconciliation_id, "reconciliation_id"),
        ):
            _uuid(value, name)
        for value, name in (
            (self.mapping_content_hash, "mapping_content_hash"),
            (self.execution_snapshot_hash, "execution_snapshot_hash"),
            (self.execution_snapshot_root_hash, "execution_snapshot_root_hash"),
            (self.execution_evidence_hash, "execution_evidence_hash"),
            (self.reconciliation_hash, "reconciliation_hash"),
            (self.target_hash, "target_hash"),
            (self.schema_hash, "schema_hash"),
            (self.read_context_hash, "read_context_hash"),
            (self.manifest_hash, "manifest_hash"),
        ):
            _hash(value, name)
        if self.mapping_version < 1:
            raise CorrectionOriginError("Correction origin mapping version is invalid")
        artifacts = tuple(
            sorted(self.prepared_artifacts, key=lambda item: item.dataset_id)
        )
        if not artifacts or artifacts != self.prepared_artifacts:
            raise CorrectionOriginError(
                "Correction prepared artifacts are not deterministic"
            )
        if len({item.dataset_id for item in artifacts}) != len(artifacts):
            raise CorrectionOriginError("Correction prepared artifacts are ambiguous")
        try:
            required_text(self.target_observed_at, "target_observed_at", maximum=100)
        except ValueError as error:
            raise CorrectionOriginError(
                "Correction target observation is invalid"
            ) from error
        require_aware(self.created_at, "created_at")
        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))

    @classmethod
    def create(cls, **values: object) -> "CorrectionOriginManifest":
        prepared = tuple(
            sorted(
                values.pop("prepared_artifacts"),  # type: ignore[arg-type]
                key=lambda item: item.dataset_id,
            )
        )
        unhashed = cls(
            **values,  # type: ignore[arg-type]
            prepared_artifacts=prepared,
            manifest_hash="sha256:" + "0" * 64,
        )
        return replace(
            unhashed,
            manifest_hash=content_hash(unhashed._meaning_dict()),
        )

    @property
    def prepared_set_hash(self) -> str:
        return content_hash(
            [item.portable_dict() for item in self.prepared_artifacts]
        )

    def protected_json(self) -> bytes:
        return canonical_json(
            {
                "contract": CORRECTION_ORIGIN_CONTRACT,
                **self._meaning_dict(),
                "manifest_hash": self.manifest_hash,
            }
        ).encode("utf-8")

    @classmethod
    def from_protected_json(cls, payload: bytes) -> "CorrectionOriginManifest":
        try:
            raw = json.loads(payload)
            if (
                not isinstance(raw, dict)
                or raw.get("contract") != CORRECTION_ORIGIN_CONTRACT
            ):
                raise CorrectionOriginError("Correction origin contract is unsupported")
            manifest = cls(
                manifest_id=str(raw["manifest_id"]),
                project_id=str(raw["project_id"]),
                data_version_id=str(raw["data_version_id"]),
                completed_migration_run_id=str(raw["completed_migration_run_id"]),
                completed_workspace_id=str(raw["completed_workspace_id"]),
                mapping_id=str(raw["mapping_id"]),
                mapping_version=int(raw["mapping_version"]),
                mapping_content_hash=str(raw["mapping_content_hash"]),
                prepared_artifacts=tuple(
                    CorrectionPreparedArtifact(
                        dataset_id=str(item["dataset_id"]),
                        dataset_name=str(item["dataset_name"]),
                        source_snapshot_hash=str(item["source_snapshot_hash"]),
                        logical_hash=str(item["logical_hash"]),
                        content_hash=str(item["content_hash"]),
                        parquet_storage_key=str(item["parquet_storage_key"]),
                        parquet_sha256=str(item["parquet_sha256"]),
                        row_count=int(item["row_count"]),
                    )
                    for item in raw["prepared_artifacts"]
                ),
                execution_snapshot_hash=str(raw["execution_snapshot_hash"]),
                execution_snapshot_root_hash=str(
                    raw["execution_snapshot_root_hash"]
                ),
                preflight_run_id=str(raw["preflight_run_id"]),
                execution_run_id=str(raw["execution_run_id"]),
                execution_evidence_hash=str(raw["execution_evidence_hash"]),
                reconciliation_id=str(raw["reconciliation_id"]),
                reconciliation_hash=str(raw["reconciliation_hash"]),
                target_hash=str(raw["target_hash"]),
                schema_hash=str(raw["schema_hash"]),
                read_context_hash=str(raw["read_context_hash"]),
                target_observed_at=str(raw["target_observed_at"]),
                target_index=ProtectedCorrectionArtifactReference(
                    artifact_id=str(raw["target_index"]["artifact_id"]),
                    logical_hash=str(raw["target_index"]["logical_hash"]),
                    storage_key=str(raw["target_index"]["storage_key"]),
                    artifact_hash=str(raw["target_index"]["artifact_hash"]),
                ),
                created_by=_actor_from_dict(raw["created_by"]),
                created_at=datetime.fromisoformat(str(raw["created_at"])),
                manifest_hash=str(raw["manifest_hash"]),
            )
        except CorrectionOriginError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CorrectionOriginError(
                "Correction origin payload is invalid"
            ) from error
        if content_hash(manifest._meaning_dict()) != manifest.manifest_hash:
            raise CorrectionOriginError("Correction origin hash changed")
        return manifest

    def _meaning_dict(self) -> dict[str, object]:
        return {
            "completed_migration_run_id": self.completed_migration_run_id,
            "completed_workspace_id": self.completed_workspace_id,
            "created_at": self.created_at.isoformat(),
            "created_by": _actor_dict(self.created_by),
            "data_version_id": self.data_version_id,
            "execution_evidence_hash": self.execution_evidence_hash,
            "execution_run_id": self.execution_run_id,
            "execution_snapshot_hash": self.execution_snapshot_hash,
            "execution_snapshot_root_hash": self.execution_snapshot_root_hash,
            "manifest_id": self.manifest_id,
            "mapping_content_hash": self.mapping_content_hash,
            "mapping_id": self.mapping_id,
            "mapping_version": self.mapping_version,
            "preflight_run_id": self.preflight_run_id,
            "prepared_artifacts": [
                item.portable_dict() for item in self.prepared_artifacts
            ],
            "project_id": self.project_id,
            "read_context_hash": self.read_context_hash,
            "reconciliation_hash": self.reconciliation_hash,
            "reconciliation_id": self.reconciliation_id,
            "schema_hash": self.schema_hash,
            "target_hash": self.target_hash,
            "target_index": self.target_index.portable_dict(),
            "target_observed_at": self.target_observed_at,
        }


def _uuid(value: str, name: str) -> None:
    try:
        require_uuid(value, name)
    except ValueError as error:
        raise CorrectionOriginError(f"Correction {name} is invalid") from error


def _hash(value: str, name: str) -> None:
    try:
        require_hash(value, name)
    except ValueError as error:
        raise CorrectionOriginError(f"Correction {name} is invalid") from error


def _actor_dict(actor: ActorIdentity) -> dict[str, str]:
    return {
        "display_name": actor.display_name,
        "issuer": actor.issuer,
        "subject_id": actor.subject_id,
    }


def _actor_from_dict(value: object) -> ActorIdentity:
    if not isinstance(value, dict):
        raise CorrectionOriginError("Correction actor identity is invalid")
    return ActorIdentity(
        issuer=str(value["issuer"]),
        subject_id=str(value["subject_id"]),
        display_name=str(value["display_name"]),
    )


__all__ = [
    "CorrectionOriginError",
    "CorrectionOriginManifest",
    "CorrectionPreparedArtifact",
    "CorrectionTargetIndex",
    "CorrectionTargetIndexEntry",
    "ProtectedCorrectionArtifactReference",
]
