"""Define immutable typed-value evidence for derived preparation outputs.

Direct one-to-one datasets reuse :class:`PreparedSnapshot`.  This separate
contract is reserved for lookup, parent, join, union, or grouped outputs whose
values, order, lineage, or row cardinality no longer match one source snapshot.
It is a manifest only; publication and route admission remain separate steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Iterable

from .serialization import canonical_json, content_hash


DERIVED_VALUE_ARTIFACT_CONTRACT_VERSION = 1
DERIVED_VALUE_ARTIFACT_STORAGE_LAYOUT_VERSION = 1
DERIVED_VALUE_WRITER_CONTRACT_VERSION = 1
DERIVED_VALUE_ORDINAL_COLUMN = "__impodo_derived_ordinal"

_DATASET_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SHA256 = re.compile(r"sha256:([0-9a-f]{64})")


class DerivedValueArtifactContractError(ValueError):
    """Raised when derived typed-value evidence violates its exact contract."""


class DerivedValueKind(StrEnum):
    """Cardinality-changing preparation operation that produced the values."""

    LOOKUP = "lookup"
    RELATED_PARENT = "related_parent"
    JOIN = "join"
    UNION = "union"
    GROUP = "group"


@dataclass(frozen=True, slots=True)
class DerivedValueInput:
    """Bind one exact source or upstream-derived value carrier."""

    dataset_id: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _dataset_id(self.dataset_id, "input dataset ID")
        _hash_digest(self.evidence_hash, "input evidence hash")

    def to_portable_dict(self) -> dict[str, str]:
        return {
            "dataset_id": self.dataset_id,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class DerivedValueArtifact:
    """Bind one typed derived Parquet artifact to all semantic inputs."""

    project_id: str
    dataset_id: str
    dataset_name: str
    derivation_kind: DerivedValueKind
    input_evidence: tuple[DerivedValueInput, ...]
    physical_selection_hash: str
    effective_selection_hash: str
    derived_plan_hash: str
    derivation_rule_hash: str
    mapping_hash: str
    schema_hash: str
    transformation_program_hash: str
    lineage_hash: str
    writer_contract_version: int
    row_count: int
    physical_schema_hash: str
    logical_hash: str
    parquet_storage_key: str
    parquet_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.project_id or len(self.project_id) > 200:
            raise DerivedValueArtifactContractError(
                "Derived value artifact project ID is invalid"
            )
        _dataset_id(self.dataset_id, "dataset ID")
        if _DATASET_NAME.fullmatch(self.dataset_name) is None:
            raise DerivedValueArtifactContractError(
                "Derived value artifact dataset name is invalid"
            )
        try:
            derivation_kind = DerivedValueKind(self.derivation_kind)
        except ValueError as error:
            raise DerivedValueArtifactContractError(
                "Derived value artifact derivation kind is invalid"
            ) from error
        object.__setattr__(self, "derivation_kind", derivation_kind)
        if not self.input_evidence:
            raise DerivedValueArtifactContractError(
                "Derived value artifact requires exact input evidence"
            )
        ordered_inputs = tuple(
            sorted(self.input_evidence, key=lambda item: item.dataset_id)
        )
        if self.input_evidence != ordered_inputs or len(
            {item.dataset_id for item in self.input_evidence}
        ) != len(self.input_evidence):
            raise DerivedValueArtifactContractError(
                "Derived value artifact inputs must be unique and ordered"
            )
        for value, label in (
            (self.physical_selection_hash, "physical selection hash"),
            (self.effective_selection_hash, "effective selection hash"),
            (self.derived_plan_hash, "derived plan hash"),
            (self.derivation_rule_hash, "derivation rule hash"),
            (self.mapping_hash, "mapping hash"),
            (self.schema_hash, "schema hash"),
            (self.transformation_program_hash, "transformation program hash"),
            (self.lineage_hash, "lineage hash"),
            (self.physical_schema_hash, "physical schema hash"),
            (self.logical_hash, "logical hash"),
            (self.parquet_sha256, "Parquet hash"),
        ):
            _hash_digest(value, label)
        if self.writer_contract_version < 1:
            raise DerivedValueArtifactContractError(
                "Derived value writer contract version must be positive"
            )
        if self.row_count < 0:
            raise DerivedValueArtifactContractError(
                "Derived value artifact row count is negative"
            )
        if self.created_at.tzinfo is None:
            raise DerivedValueArtifactContractError(
                "Derived value artifact creation time must be timezone-aware"
            )
        if self.logical_hash != self.expected_logical_hash:
            raise DerivedValueArtifactContractError(
                "Derived value artifact logical hash is invalid"
            )
        if self.parquet_storage_key != derived_value_artifact_storage_key(
            self.dataset_id,
            self.logical_hash,
            self.parquet_sha256,
        ):
            raise DerivedValueArtifactContractError(
                "Derived value artifact storage key is not content-addressed"
            )

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        dataset_id: str,
        dataset_name: str,
        derivation_kind: DerivedValueKind,
        input_evidence: Iterable[DerivedValueInput],
        physical_selection_hash: str,
        effective_selection_hash: str,
        derived_plan_hash: str,
        derivation_rule_hash: str,
        mapping_hash: str,
        schema_hash: str,
        transformation_program_hash: str,
        lineage_hash: str,
        row_count: int,
        physical_schema_hash: str,
        parquet_sha256: str,
        created_at: datetime,
        writer_contract_version: int = DERIVED_VALUE_WRITER_CONTRACT_VERSION,
    ) -> "DerivedValueArtifact":
        ordered_inputs = tuple(sorted(input_evidence, key=lambda item: item.dataset_id))
        logical_hash = derived_value_artifact_logical_hash(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            derivation_kind=derivation_kind,
            input_evidence=ordered_inputs,
            physical_selection_hash=physical_selection_hash,
            effective_selection_hash=effective_selection_hash,
            derived_plan_hash=derived_plan_hash,
            derivation_rule_hash=derivation_rule_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            transformation_program_hash=transformation_program_hash,
            lineage_hash=lineage_hash,
            writer_contract_version=writer_contract_version,
            row_count=row_count,
        )
        return cls(
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_name=dataset_name,
            derivation_kind=derivation_kind,
            input_evidence=ordered_inputs,
            physical_selection_hash=physical_selection_hash,
            effective_selection_hash=effective_selection_hash,
            derived_plan_hash=derived_plan_hash,
            derivation_rule_hash=derivation_rule_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            transformation_program_hash=transformation_program_hash,
            lineage_hash=lineage_hash,
            writer_contract_version=writer_contract_version,
            row_count=row_count,
            physical_schema_hash=physical_schema_hash,
            logical_hash=logical_hash,
            parquet_storage_key=derived_value_artifact_storage_key(
                dataset_id,
                logical_hash,
                parquet_sha256,
            ),
            parquet_sha256=parquet_sha256,
            created_at=created_at,
        )

    @property
    def expected_logical_hash(self) -> str:
        return derived_value_artifact_logical_hash(
            project_id=self.project_id,
            dataset_id=self.dataset_id,
            dataset_name=self.dataset_name,
            derivation_kind=self.derivation_kind,
            input_evidence=self.input_evidence,
            physical_selection_hash=self.physical_selection_hash,
            effective_selection_hash=self.effective_selection_hash,
            derived_plan_hash=self.derived_plan_hash,
            derivation_rule_hash=self.derivation_rule_hash,
            mapping_hash=self.mapping_hash,
            schema_hash=self.schema_hash,
            transformation_program_hash=self.transformation_program_hash,
            lineage_hash=self.lineage_hash,
            writer_contract_version=self.writer_contract_version,
            row_count=self.row_count,
        )

    @property
    def content_hash(self) -> str:
        """Bind semantic derivation identity to exact physical artifact bytes."""

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
            "contract_version": DERIVED_VALUE_ARTIFACT_CONTRACT_VERSION,
            "created_at": self.created_at.isoformat(),
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "derivation_kind": self.derivation_kind.value,
            "derivation_rule_hash": self.derivation_rule_hash,
            "derived_plan_hash": self.derived_plan_hash,
            "effective_selection_hash": self.effective_selection_hash,
            "input_evidence": [
                item.to_portable_dict() for item in self.input_evidence
            ],
            "lineage_hash": self.lineage_hash,
            "logical_hash": self.logical_hash,
            "mapping_hash": self.mapping_hash,
            "parquet_sha256": self.parquet_sha256,
            "parquet_storage_key": self.parquet_storage_key,
            "physical_schema_hash": self.physical_schema_hash,
            "physical_selection_hash": self.physical_selection_hash,
            "project_id": self.project_id,
            "row_count": self.row_count,
            "schema_hash": self.schema_hash,
            "transformation_program_hash": self.transformation_program_hash,
            "writer_contract_version": self.writer_contract_version,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_portable_dict())

    @classmethod
    def from_json(cls, value: str) -> "DerivedValueArtifact":
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict):
                raise TypeError
            if (
                int(payload["contract_version"])
                != DERIVED_VALUE_ARTIFACT_CONTRACT_VERSION
            ):
                raise ValueError
            artifact = cls(
                project_id=str(payload["project_id"]),
                dataset_id=str(payload["dataset_id"]),
                dataset_name=str(payload["dataset_name"]),
                derivation_kind=DerivedValueKind(str(payload["derivation_kind"])),
                input_evidence=tuple(
                    DerivedValueInput(
                        dataset_id=str(item["dataset_id"]),
                        evidence_hash=str(item["evidence_hash"]),
                    )
                    for item in payload["input_evidence"]
                ),
                physical_selection_hash=str(payload["physical_selection_hash"]),
                effective_selection_hash=str(payload["effective_selection_hash"]),
                derived_plan_hash=str(payload["derived_plan_hash"]),
                derivation_rule_hash=str(payload["derivation_rule_hash"]),
                mapping_hash=str(payload["mapping_hash"]),
                schema_hash=str(payload["schema_hash"]),
                transformation_program_hash=str(
                    payload["transformation_program_hash"]
                ),
                lineage_hash=str(payload["lineage_hash"]),
                writer_contract_version=int(payload["writer_contract_version"]),
                row_count=int(payload["row_count"]),
                physical_schema_hash=str(payload["physical_schema_hash"]),
                logical_hash=str(payload["logical_hash"]),
                parquet_storage_key=str(payload["parquet_storage_key"]),
                parquet_sha256=str(payload["parquet_sha256"]),
                created_at=datetime.fromisoformat(str(payload["created_at"])),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            if isinstance(error, DerivedValueArtifactContractError):
                raise
            raise DerivedValueArtifactContractError(
                "Derived value artifact manifest is invalid"
            ) from error
        if payload.get("content_hash") != artifact.content_hash:
            raise DerivedValueArtifactContractError(
                "Derived value artifact manifest content hash is invalid"
            )
        return artifact


def derived_value_artifact_logical_hash(
    *,
    project_id: str,
    dataset_id: str,
    dataset_name: str,
    derivation_kind: DerivedValueKind,
    input_evidence: Iterable[DerivedValueInput],
    physical_selection_hash: str,
    effective_selection_hash: str,
    derived_plan_hash: str,
    derivation_rule_hash: str,
    mapping_hash: str,
    schema_hash: str,
    transformation_program_hash: str,
    lineage_hash: str,
    writer_contract_version: int,
    row_count: int,
) -> str:
    """Hash exact logical derivation inputs independently of Parquet encoding."""

    return content_hash(
        {
            "contract_version": DERIVED_VALUE_ARTIFACT_CONTRACT_VERSION,
            "dataset_id": dataset_id,
            "dataset_name": dataset_name,
            "derivation_kind": DerivedValueKind(derivation_kind).value,
            "derivation_rule_hash": derivation_rule_hash,
            "derived_plan_hash": derived_plan_hash,
            "effective_selection_hash": effective_selection_hash,
            "input_evidence": [
                item.to_portable_dict()
                for item in sorted(input_evidence, key=lambda item: item.dataset_id)
            ],
            "lineage_hash": lineage_hash,
            "mapping_hash": mapping_hash,
            "physical_selection_hash": physical_selection_hash,
            "project_id": project_id,
            "row_count": row_count,
            "schema_hash": schema_hash,
            "transformation_program_hash": transformation_program_hash,
            "writer_contract_version": writer_contract_version,
        }
    )


def derived_value_artifact_storage_key(
    dataset_id: str,
    logical_hash: str,
    parquet_sha256: str,
) -> str:
    """Return a safe content-addressed key for one derived Parquet artifact."""

    _dataset_id(dataset_id, "dataset ID")
    logical_digest = _hash_digest(logical_hash, "logical hash")
    artifact_digest = _hash_digest(parquet_sha256, "Parquet hash")
    binding_digest = _hash_digest(
        content_hash(
            {
                "logical_hash": f"sha256:{logical_digest}",
                "parquet_sha256": f"sha256:{artifact_digest}",
            }
        ),
        "artifact storage binding",
    )
    dataset_segment = sha256(dataset_id.encode("utf-8")).hexdigest()[:24]
    return str(
        PurePosixPath(
            "snapshots",
            "derived",
            f"v{DERIVED_VALUE_ARTIFACT_STORAGE_LAYOUT_VERSION}",
            dataset_segment,
            f"{binding_digest}.parquet",
        )
    )


def _dataset_id(value: str, label: str) -> str:
    clean = str(value).strip()
    if (
        clean != value
        or not clean
        or len(clean) > 200
        or any(ord(character) < 32 for character in clean)
    ):
        raise DerivedValueArtifactContractError(
            f"Derived value artifact {label} is invalid"
        )
    return clean


def _hash_digest(value: str, label: str) -> str:
    match = _SHA256.fullmatch(value)
    if match is None:
        raise DerivedValueArtifactContractError(
            f"Derived value artifact {label} is invalid"
        )
    return match.group(1)


__all__ = [
    "DERIVED_VALUE_ARTIFACT_CONTRACT_VERSION",
    "DERIVED_VALUE_ARTIFACT_STORAGE_LAYOUT_VERSION",
    "DERIVED_VALUE_ORDINAL_COLUMN",
    "DERIVED_VALUE_WRITER_CONTRACT_VERSION",
    "DerivedValueArtifact",
    "DerivedValueArtifactContractError",
    "DerivedValueInput",
    "DerivedValueKind",
    "derived_value_artifact_logical_hash",
    "derived_value_artifact_storage_key",
]
