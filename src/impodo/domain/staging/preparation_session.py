"""Storage-independent contracts for bounded Stage-E preparation sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ..compiler.columnar_transformation import ColumnarTransformationProgram
from ...models import (
    Issue,
    PreparedRecord,
    Severity,
    portable_issue,
    portable_value,
    restore_portable_value,
)
from ...staging_contracts import (
    CanonicalControlTotal,
    CanonicalIssue,
    CanonicalRow,
    StagingDatasetReconciliation,
    StagingDisposition,
    StagingReconciliation,
)
from .transformation_impact import TransformationImpactReport, TransformationImpactRow


class PreparationSessionStatus(StrEnum):
    """Lifecycle of unpublished, restart-safe preparation evidence."""

    BUILDING = "BUILDING"
    FINALIZING = "FINALIZING"
    READY = "READY"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PreparationSessionBindings:
    """Immutable inputs and evaluator versions bound when a session begins."""

    mapping_id: str
    mapping_version: int
    physical_selection_hash: str
    source_selection_hash: str
    mapping_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    compiled_plan_hash: str
    contract_version: int
    evaluator_version: int
    source_hashes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PreparationSessionSummary:
    """Small status projection that contains no source values."""

    session_id: str
    status: PreparationSessionStatus
    bindings: PreparationSessionBindings
    provisional_row_count: int = 0
    canonical_row_count: int = 0
    impact_row_count: int = 0
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedSessionRow:
    """One provisional prepared record plus normalized physical lineage."""

    record: PreparedRecord
    physical_sources: Mapping[str, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class CanonicalPreparedSessionRow:
    """Compact already-encoded canonical payload for the direct fast path."""

    row_id: str
    ordinal: int
    dataset: str
    source_row: int
    target_model: str
    disposition: StagingDisposition
    source_identity: tuple[Any, ...]
    row_json: str
    physical_sources: Mapping[str, tuple[int, ...]]
    references: Mapping[str, Any] = field(default_factory=dict)
    record_label: str = ""
    quality_identity_key: str | None = None
    issues: tuple[CanonicalIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedCanonicalProjection:
    """Versioned recipe for reconstructing direct canonical value rows."""

    dataset_id: str
    dataset: str
    ordinal_start: int
    row_count: int
    mode: str
    source_hash: str
    physical_dataset_id: str
    field_sources: Mapping[str, tuple[str, ...]]
    program: ColumnarTransformationProgram
    contract_version: int = 1

    def __post_init__(self) -> None:
        if (
            self.dataset_id != self.program.dataset_id
            or self.dataset != self.program.dataset_name
            or self.mode != self.program.target_mode
            or self.physical_dataset_id != self.dataset_id
            or self.ordinal_start < 0
            or self.row_count < 0
            or self.contract_version != 1
        ):
            raise ValueError("Prepared canonical projection is invalid")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dataset_id": self.dataset_id,
            "dataset": self.dataset,
            "ordinal_start": self.ordinal_start,
            "row_count": self.row_count,
            "mode": self.mode,
            "source_hash": self.source_hash,
            "physical_dataset_id": self.physical_dataset_id,
            "field_sources": {
                field: list(sources)
                for field, sources in sorted(self.field_sources.items())
            },
            "program": self.program.to_portable_dict(),
            "program_hash": self.program.content_hash,
        }

    @classmethod
    def from_portable_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "PreparedCanonicalProjection":
        program_payload = payload.get("program")
        if not isinstance(program_payload, Mapping):
            raise ValueError("Prepared canonical projection program is invalid")
        program = ColumnarTransformationProgram.from_portable_dict(
            program_payload
        )
        if payload.get("program_hash") != program.content_hash:
            raise ValueError("Prepared canonical projection program changed")
        raw_field_sources = payload.get("field_sources", {})
        if not isinstance(raw_field_sources, Mapping):
            raise ValueError("Prepared canonical field sources are invalid")
        return cls(
            dataset_id=str(payload["dataset_id"]),
            dataset=str(payload["dataset"]),
            ordinal_start=int(payload["ordinal_start"]),
            row_count=int(payload["row_count"]),
            mode=str(payload["mode"]),
            source_hash=str(payload["source_hash"]),
            physical_dataset_id=str(payload["physical_dataset_id"]),
            field_sources={
                str(field): tuple(str(source) for source in sources)
                for field, sources in raw_field_sources.items()
            },
            program=program,
            contract_version=int(payload["contract_version"]),
        )


@dataclass(frozen=True, slots=True)
class StoredCanonicalStagingRun:
    """Validated canonical header backed by a bounded durable row sequence.

    It deliberately matches the publication attributes of
    ``CanonicalStagingRun`` without requiring every row to be resident at once.
    """

    project_id: str
    mapping_id: str
    physical_selection_hash: str
    source_selection_hash: str
    mapping_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    datasets: tuple[StagingDatasetReconciliation, ...]
    rows: Sequence[CanonicalRow]
    issues: tuple[CanonicalIssue, ...]
    reconciliation: StagingReconciliation
    compiled_plan_hash: str
    control_totals: tuple[CanonicalControlTotal, ...]
    evaluator_version: int
    contract_version: int
    validated_content_hash: str | None = None


def prepared_record_to_portable_dict(record: PreparedRecord) -> dict[str, Any]:
    """Encode one typed provisional record without losing reference meaning."""

    return {
        "dataset": record.dataset,
        "source_row": record.source_row,
        "target_model": record.target_model,
        "source_identity": portable_value(record.source_identity),
        "target_identity": portable_value(record.target_identity),
        "target_scope": portable_value(record.target_scope),
        "scalar_values": portable_value(record.scalar_values),
        "references": portable_value(record.references),
        "source_trace_id": record.source_trace_id,
        "issues": [portable_issue(issue) for issue in record.issues],
    }


def prepared_record_from_portable_dict(
    payload: Mapping[str, Any],
) -> PreparedRecord:
    """Restore one provisional record and validate immutable collection shapes."""

    source_identity = restore_portable_value(payload.get("source_identity", ()))
    target_identity = restore_portable_value(payload.get("target_identity", ()))
    target_scope = restore_portable_value(payload.get("target_scope", ()))
    scalar_values = restore_portable_value(payload.get("scalar_values", {}))
    references = restore_portable_value(payload.get("references", {}))
    if not all(
        isinstance(value, tuple)
        for value in (source_identity, target_identity, target_scope)
    ):
        raise ValueError("Prepared session identities are invalid")
    if not isinstance(scalar_values, dict) or not isinstance(references, dict):
        raise ValueError("Prepared session values are invalid")
    return PreparedRecord(
        dataset=str(payload["dataset"]),
        source_row=int(payload["source_row"]),
        target_model=str(payload["target_model"]),
        source_identity=source_identity,
        target_identity=target_identity,
        target_scope=target_scope,
        scalar_values=scalar_values,
        references=references,
        source_trace_id=str(payload.get("source_trace_id", "")),
        issues=tuple(
            issue_from_portable_dict(item)
            for item in payload.get("issues", ())
        ),
    )


def issue_from_portable_dict(payload: Mapping[str, Any]) -> Issue:
    """Restore existing structured issue evidence from session storage."""

    return Issue(
        code=str(payload["code"]),
        message=str(payload["message"]),
        severity=Severity(str(payload.get("severity", Severity.ERROR.value))),
        dataset=(str(payload["dataset"]) if payload.get("dataset") else None),
        row=(int(payload["row"]) if payload.get("row") is not None else None),
        field=(str(payload["field"]) if payload.get("field") else None),
        affected_count=int(payload.get("affected_count", 1)),
    )


def transformation_impact_to_portable_dict(
    row: TransformationImpactRow,
) -> dict[str, Any]:
    """Encode one already-display-safe transformation impact row."""

    return {
        "dataset": row.dataset,
        "source_row": row.source_row,
        "source_column": row.source_column,
        "target_field": row.target_field,
        "raw_value": row.raw_value,
        "proposed_value": row.proposed_value,
        "rules": row.rules,
        "outcome": row.outcome,
        "message": row.message,
    }


def transformation_impact_from_portable_dict(
    payload: Mapping[str, Any],
) -> TransformationImpactRow:
    """Restore one transformation impact for normalization consumption."""

    return TransformationImpactRow(
        dataset=str(payload["dataset"]),
        source_row=int(payload["source_row"]),
        source_column=str(payload["source_column"]),
        target_field=str(payload["target_field"]),
        raw_value=str(payload["raw_value"]),
        proposed_value=str(payload["proposed_value"]),
        rules=str(payload["rules"]),
        outcome=str(payload["outcome"]),
        message=str(payload.get("message", "")),
    )


def transformation_report_to_portable_dict(
    report: TransformationImpactReport,
) -> dict[str, Any]:
    """Persist complete counters while detail rows remain in their own table."""

    return {
        "mapping_content_hash": report.mapping_content_hash,
        "evaluated_count": report.evaluated_count,
        "changed_count": report.changed_count,
        "fallback_count": report.fallback_count,
        "null_count": report.null_count,
        "invalid_count": report.invalid_count,
        "provided_count": report.provided_count,
        "unchanged_count": report.unchanged_count,
        "detail_limit": report.detail_limit,
    }
