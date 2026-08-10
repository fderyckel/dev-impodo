"""Define immutable mapping-bound Parquet evidence for native preparation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import PurePosixPath
import re

from .serialization import canonical_json, content_hash


PREPARED_SNAPSHOT_CONTRACT_VERSION = 1
PREPARED_SNAPSHOT_STORAGE_LAYOUT_VERSION = 2
PREPARED_WRITER_CONTRACT_VERSION = 1

_DATASET_ID = re.compile(r"dataset:([0-9a-f]{24})")
_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")


class PreparedSnapshotContractError(ValueError):
    """Raised when prepared columnar evidence violates its exact contract."""


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    """Bind one typed prepared Parquet artifact to exact transformation inputs."""

    project_id: str
    dataset_id: str
    dataset_name: str
    source_snapshot_hash: str
    mapping_hash: str
    schema_hash: str
    transformation_program_hash: str
    writer_contract_version: int
    row_count: int
    physical_schema_hash: str
    logical_hash: str
    parquet_storage_key: str
    parquet_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.project_id or not self.dataset_name:
            raise PreparedSnapshotContractError(
                "Prepared snapshot project and dataset names are required"
            )
        _dataset_digest(self.dataset_id)
        for value, label in (
            (self.source_snapshot_hash, "source snapshot hash"),
            (self.mapping_hash, "mapping hash"),
            (self.schema_hash, "schema hash"),
            (self.transformation_program_hash, "transformation program hash"),
            (self.physical_schema_hash, "physical schema hash"),
            (self.logical_hash, "logical hash"),
            (self.parquet_sha256, "Parquet hash"),
        ):
            _hash_digest(value, label)
        if self.writer_contract_version < 1:
            raise PreparedSnapshotContractError(
                "Prepared writer contract version must be positive"
            )
        if self.row_count < 0:
            raise PreparedSnapshotContractError(
                "Prepared snapshot row count is negative"
            )
        if self.created_at.tzinfo is None:
            raise PreparedSnapshotContractError(
                "Prepared snapshot creation time must be timezone-aware"
            )
        if self.logical_hash != self.expected_logical_hash:
            raise PreparedSnapshotContractError(
                "Prepared snapshot logical hash is invalid"
            )
        if self.parquet_storage_key != prepared_snapshot_storage_key(
            self.dataset_id,
            self.logical_hash,
            self.parquet_sha256,
        ):
            raise PreparedSnapshotContractError(
                "Prepared snapshot storage key is not content-addressed"
            )

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        dataset_id: str,
        dataset_name: str,
        source_snapshot_hash: str,
        mapping_hash: str,
        schema_hash: str,
        transformation_program_hash: str,
        row_count: int,
        physical_schema_hash: str,
        parquet_sha256: str,
        created_at: datetime,
        writer_contract_version: int = PREPARED_WRITER_CONTRACT_VERSION,
    ) -> "PreparedSnapshot":
        logical_hash = prepared_snapshot_logical_hash(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            source_snapshot_hash=source_snapshot_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            transformation_program_hash=transformation_program_hash,
            writer_contract_version=writer_contract_version,
            row_count=row_count,
        )
        return cls(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            source_snapshot_hash=source_snapshot_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            transformation_program_hash=transformation_program_hash,
            writer_contract_version=writer_contract_version,
            row_count=row_count,
            physical_schema_hash=physical_schema_hash,
            logical_hash=logical_hash,
            parquet_storage_key=prepared_snapshot_storage_key(
                dataset_id,
                logical_hash,
                parquet_sha256,
            ),
            parquet_sha256=parquet_sha256,
            created_at=created_at,
        )

    @property
    def expected_logical_hash(self) -> str:
        return prepared_snapshot_logical_hash(
            project_id=self.project_id,
            dataset_id=self.dataset_id,
            dataset_name=self.dataset_name,
            source_snapshot_hash=self.source_snapshot_hash,
            mapping_hash=self.mapping_hash,
            schema_hash=self.schema_hash,
            transformation_program_hash=self.transformation_program_hash,
            writer_contract_version=self.writer_contract_version,
            row_count=self.row_count,
        )

    @property
    def content_hash(self) -> str:
        """Bind logical transformation inputs to exact physical output bytes."""

        return content_hash(
            {
                "logical_hash": self.logical_hash,
                "parquet_sha256": self.parquet_sha256,
                "parquet_storage_key": self.parquet_storage_key,
                "physical_schema_hash": self.physical_schema_hash,
            }
        )

    def to_portable_dict(self) -> dict[str, object]:
        return {
            "content_hash": self.content_hash,
            "contract_version": PREPARED_SNAPSHOT_CONTRACT_VERSION,
            "created_at": self.created_at.isoformat(),
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "logical_hash": self.logical_hash,
            "mapping_hash": self.mapping_hash,
            "parquet_sha256": self.parquet_sha256,
            "parquet_storage_key": self.parquet_storage_key,
            "physical_schema_hash": self.physical_schema_hash,
            "project_id": self.project_id,
            "row_count": self.row_count,
            "schema_hash": self.schema_hash,
            "source_snapshot_hash": self.source_snapshot_hash,
            "transformation_program_hash": self.transformation_program_hash,
            "writer_contract_version": self.writer_contract_version,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_portable_dict())

    @classmethod
    def from_json(cls, value: str) -> "PreparedSnapshot":
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                raise TypeError
            if int(payload["contract_version"]) != PREPARED_SNAPSHOT_CONTRACT_VERSION:
                raise ValueError
            snapshot = cls(
                project_id=str(payload["project_id"]),
                dataset_id=str(payload["dataset_id"]),
                dataset_name=str(payload["dataset_name"]),
                source_snapshot_hash=str(payload["source_snapshot_hash"]),
                mapping_hash=str(payload["mapping_hash"]),
                schema_hash=str(payload["schema_hash"]),
                transformation_program_hash=str(
                    payload["transformation_program_hash"]
                ),
                writer_contract_version=int(payload["writer_contract_version"]),
                row_count=int(payload["row_count"]),
                physical_schema_hash=str(payload["physical_schema_hash"]),
                logical_hash=str(payload["logical_hash"]),
                parquet_storage_key=str(payload["parquet_storage_key"]),
                parquet_sha256=str(payload["parquet_sha256"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            if isinstance(error, PreparedSnapshotContractError):
                raise
            raise PreparedSnapshotContractError(
                "Prepared snapshot manifest is invalid"
            ) from error
        if payload.get("content_hash") != snapshot.content_hash:
            raise PreparedSnapshotContractError(
                "Prepared snapshot manifest content hash is invalid"
            )
        return snapshot


def prepared_snapshot_logical_hash(
    *,
    project_id: str,
    dataset_id: str,
    dataset_name: str,
    source_snapshot_hash: str,
    mapping_hash: str,
    schema_hash: str,
    transformation_program_hash: str,
    writer_contract_version: int,
    row_count: int,
) -> str:
    """Hash exact transformation inputs independently of Parquet encoding."""

    return content_hash(
        {
            "contract_version": PREPARED_SNAPSHOT_CONTRACT_VERSION,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "mapping_hash": mapping_hash,
            "project_id": project_id,
            "row_count": row_count,
            "schema_hash": schema_hash,
            "source_snapshot_hash": source_snapshot_hash,
            "transformation_program_hash": transformation_program_hash,
            "writer_contract_version": writer_contract_version,
        }
    )


def prepared_snapshot_storage_key(
    dataset_id: str,
    logical_hash: str,
    parquet_sha256: str,
) -> str:
    """Return a safe application-constructed project-relative artifact key."""

    logical_digest = _hash_digest(logical_hash, "logical hash")
    artifact_digest = _hash_digest(parquet_sha256, "Parquet hash")
    binding_digest = _hash_digest(
        content_hash(
            {
                "logical_hash": f"sha256:{logical_digest}",
                "parquet_sha256": f"sha256:{artifact_digest}",
            }
        ),
        "snapshot storage binding",
    )
    return str(
        PurePosixPath(
            "snapshots",
            "prepared",
            f"v{PREPARED_SNAPSHOT_STORAGE_LAYOUT_VERSION}",
            _dataset_digest(dataset_id),
            f"{binding_digest}.parquet",
        )
    )


def _dataset_digest(value: str) -> str:
    match = _DATASET_ID.fullmatch(value)
    if match is None:
        raise PreparedSnapshotContractError("Prepared snapshot dataset ID is invalid")
    return match.group(1)


def _hash_digest(value: str, label: str) -> str:
    match = _SHA256.fullmatch(value)
    if match is None:
        raise PreparedSnapshotContractError(
            f"Prepared snapshot {label} is invalid"
        )
    return match.group(1)
