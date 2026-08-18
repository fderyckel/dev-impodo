"""Versioned, portable contracts for target-independent canonical evaluation.

These objects describe the deterministic output of applying one exact mapping
to every frozen source row.  They deliberately stop before Odoo resolution,
approval, persistence, or execution.  Numeric Odoo identifiers are forbidden
from every portable payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Any, Iterable, Mapping

from .models import (
    Issue,
    PreparedRecord,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
    portable_issue,
    portable_value,
    restore_portable_value,
)
from .domain.compiler.contracts import CompiledMigrationPlan
from .source import PreparedBundle


STAGING_CONTRACT_VERSION = 5
BROWSER_EVALUATOR_VERSION = 5
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class StagingDisposition(StrEnum):
    """One reconciled source-side outcome before target comparison."""

    CANDIDATE = "CANDIDATE"
    REFERENCE = "REFERENCE"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    EXCLUDED = "EXCLUDED"


class StagingDatasetRole(StrEnum):
    """How one effective dataset was produced from frozen source rows."""

    DIRECT = "DIRECT"
    PARENT = "PARENT"
    CHILD = "CHILD"
    LOOKUP = "LOOKUP"
    JOIN = "JOIN"
    UNION = "UNION"
    GROUP = "GROUP"


def _count_dispositions(
    rows: Iterable[CanonicalRow],
) -> dict[StagingDisposition, int]:
    counts = dict.fromkeys(StagingDisposition, 0)
    for row in rows:
        counts[row.disposition] += 1
    return counts


@dataclass(frozen=True, slots=True)
class CanonicalControlTotal:
    """Deterministic result of one explicitly declared expected sum."""

    control_id: str
    name: str
    dataset: str
    target_field: str
    expected_total: str
    actual_total: str
    tolerance: str
    unit: str
    included_rows: int
    empty_rows: int

    def __post_init__(self) -> None:
        _require_hash(self.control_id, "control_id")
        if not self.name or not self.dataset or not self.target_field:
            raise ValueError("Control-total evidence is incomplete")
        if self.included_rows < 0 or self.empty_rows < 0:
            raise ValueError("Control-total row counts cannot be negative")
        try:
            expected = Decimal(self.expected_total)
            actual = Decimal(self.actual_total)
            tolerance = Decimal(self.tolerance)
        except InvalidOperation as error:
            raise ValueError("Control-total evidence must be numeric") from error
        if not expected.is_finite() or not actual.is_finite() or not tolerance.is_finite():
            raise ValueError("Control-total evidence must be finite")
        if tolerance < 0:
            raise ValueError("Control-total tolerance cannot be negative")

    @property
    def difference(self) -> str:
        """Return ``actual - expected`` using canonical decimal formatting."""

        return format(
            Decimal(self.actual_total) - Decimal(self.expected_total),
            "f",
        )

    @property
    def passed(self) -> bool:
        """Whether no values are empty and the difference is within tolerance."""

        return (
            self.empty_rows == 0
            and abs(Decimal(self.difference)) <= Decimal(self.tolerance)
        )

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize declared totals together with their derived result."""

        return {
            "control_id": self.control_id,
            "name": self.name,
            "dataset": self.dataset,
            "target_field": self.target_field,
            "expected_total": self.expected_total,
            "actual_total": self.actual_total,
            "difference": self.difference,
            "tolerance": self.tolerance,
            "unit": self.unit,
            "included_rows": self.included_rows,
            "empty_rows": self.empty_rows,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalControlTotal":
        """Load a control total and verify any persisted derived fields."""

        result = cls(
            control_id=str(payload["control_id"]),
            name=str(payload["name"]),
            dataset=str(payload["dataset"]),
            target_field=str(payload["target_field"]),
            expected_total=str(payload["expected_total"]),
            actual_total=str(payload["actual_total"]),
            tolerance=str(payload.get("tolerance", "0")),
            unit=str(payload.get("unit", "")),
            included_rows=int(payload.get("included_rows", 0)),
            empty_rows=int(payload.get("empty_rows", 0)),
        )
        if "difference" in payload and str(payload["difference"]) != result.difference:
            raise ValueError("Control-total difference is invalid")
        if "passed" in payload and bool(payload["passed"]) != result.passed:
            raise ValueError("Control-total status is invalid")
        return result


@dataclass(frozen=True, slots=True)
class CanonicalIssue:
    """Portable row or run issue retained with canonical evidence."""

    code: str
    message: str
    severity: str
    dataset: str | None = None
    source_row: int | None = None
    field: str | None = None
    affected_count: int = 1

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("Canonical issues require a code and message")
        if self.severity not in {"warning", "error"}:
            raise ValueError("Canonical issue severity is unsupported")
        if self.source_row is not None and self.source_row < 1:
            raise ValueError("Canonical issue source row must be positive")
        if self.affected_count < 1:
            raise ValueError("Canonical issue affected count must be positive")

    @property
    def blocking(self) -> bool:
        """Whether this issue prevents the row/run from progressing."""

        return self.severity == "error"

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize issue coordinates and severity without runtime objects."""

        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "field": self.field,
            "affected_count": self.affected_count,
        }

    @classmethod
    def from_issue(cls, issue: Issue) -> "CanonicalIssue":
        """Adapt the preparation model's ``Issue`` into portable evidence."""

        payload = portable_issue(issue)
        return cls(
            code=str(payload["code"]),
            message=str(payload["message"]),
            severity=str(payload["severity"]),
            dataset=(str(payload["dataset"]) if payload["dataset"] else None),
            source_row=(int(payload["row"]) if payload["row"] is not None else None),
            field=(str(payload["field"]) if payload["field"] else None),
            affected_count=int(payload["affected_count"]),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalIssue":
        """Reconstruct and validate an issue from persisted evidence."""

        return cls(
            code=str(payload["code"]),
            message=str(payload["message"]),
            severity=str(payload["severity"]),
            dataset=(str(payload["dataset"]) if payload.get("dataset") else None),
            source_row=(
                int(payload["source_row"])
                if payload.get("source_row") is not None
                else None
            ),
            field=(str(payload["field"]) if payload.get("field") else None),
            affected_count=int(payload.get("affected_count", 1)),
        )


@dataclass(frozen=True, slots=True)
class CanonicalLineage:
    """Evidence linking one canonical row and its values to frozen inputs."""

    source_selection_hash: str
    source_hash: str
    mapping_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    dataset: str
    source_row: int
    physical_dataset_id: str
    physical_source_rows: tuple[int, ...]
    field_sources: Mapping[str, tuple[str, ...]]
    physical_sources: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.physical_dataset_id:
            raise ValueError("Canonical lineage requires a physical dataset")
        if not self.physical_source_rows:
            raise ValueError("Canonical lineage requires a physical source row")
        if any(item < 1 for item in self.physical_source_rows):
            raise ValueError("Canonical physical source rows must be positive")
        if self.physical_source_rows != tuple(
            sorted(set(self.physical_source_rows))
        ):
            raise ValueError(
                "Canonical physical source rows must be unique and ordered"
            )
        sources = (
            dict(self.physical_sources)
            if self.physical_sources
            else {self.physical_dataset_id: self.physical_source_rows}
        )
        if self.physical_dataset_id not in sources:
            raise ValueError("Canonical primary source is absent from physical lineage")
        if tuple(sources[self.physical_dataset_id]) != self.physical_source_rows:
            # Keep the primary coordinate and complete lineage map synchronized
            # when callers replace one side of the redundant current invariant.
            sources[self.physical_dataset_id] = self.physical_source_rows
        normalized: dict[str, tuple[int, ...]] = {}
        for dataset_id, source_rows in sorted(sources.items()):
            ordered = tuple(sorted(set(source_rows)))
            if (
                not dataset_id
                or not ordered
                or ordered != tuple(source_rows)
                or any(item < 1 for item in ordered)
            ):
                raise ValueError("Canonical physical source lineage is invalid")
            normalized[dataset_id] = ordered
        object.__setattr__(self, "physical_sources", normalized)

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize row- and field-level links back to frozen source input."""

        payload = {
            "source_selection_hash": self.source_selection_hash,
            "source_hash": self.source_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "derived_plan_hash": self.derived_plan_hash,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "physical_dataset_id": self.physical_dataset_id,
            "physical_source_rows": list(self.physical_source_rows),
            "field_sources": {
                field: list(sources)
                for field, sources in sorted(self.field_sources.items())
            },
        }
        if len(self.physical_sources) > 1:
            payload["physical_sources"] = {
                dataset_id: list(source_rows)
                for dataset_id, source_rows in sorted(self.physical_sources.items())
            }
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalLineage":
        """Reconstruct lineage used by accounting, review, and audit."""

        return cls(
            source_selection_hash=str(payload["source_selection_hash"]),
            source_hash=str(payload["source_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            derived_plan_hash=(
                str(payload["derived_plan_hash"])
                if payload.get("derived_plan_hash")
                else None
            ),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            physical_dataset_id=str(payload["physical_dataset_id"]),
            physical_source_rows=tuple(
                int(item) for item in payload.get("physical_source_rows", ())
            ),
            field_sources={
                str(field): tuple(str(item) for item in sources)
                for field, sources in dict(payload.get("field_sources", {})).items()
            },
            physical_sources={
                str(dataset_id): tuple(int(item) for item in source_rows)
                for dataset_id, source_rows in dict(
                    payload.get("physical_sources", {})
                ).items()
            },
        )


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """One target-independent canonical row with typed proposed values.

    Stage E creates this record; Stage F overlays its quality disposition and
    Stage G hashes eligible rows. References remain logical business keys—Odoo
    numeric IDs are forbidden until the later target-resolution stage.
    """

    row_id: str
    dataset: str
    source_row: int
    target_model: str
    disposition: StagingDisposition
    source_identity: tuple[Any, ...]
    target_identity: tuple[Any, ...]
    target_scope: tuple[Any, ...]
    proposed_values: Mapping[str, Any]
    references: Mapping[str, Any]
    issues: tuple[CanonicalIssue, ...]
    lineage: CanonicalLineage

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize typed values, issues, references, and source lineage."""

        return {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "target_model": self.target_model,
            "disposition": self.disposition.value,
            "source_identity": portable_value(self.source_identity),
            "target_identity": portable_value(self.target_identity),
            "target_scope": portable_value(self.target_scope),
            "proposed_values": portable_value(self.proposed_values),
            "references": portable_value(self.references),
            "issues": [item.to_portable_dict() for item in self.issues],
            "lineage": self.lineage.to_portable_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalRow":
        """Restore portable values and reconstruct one canonical row."""

        source_identity = restore_portable_value(payload.get("source_identity", ()))
        target_identity = restore_portable_value(payload.get("target_identity", ()))
        target_scope = restore_portable_value(payload.get("target_scope", ()))
        proposed_values = restore_portable_value(payload.get("proposed_values", {}))
        references = restore_portable_value(payload.get("references", {}))
        if not all(
            isinstance(item, tuple)
            for item in (source_identity, target_identity, target_scope)
        ) or not isinstance(proposed_values, dict) or not isinstance(references, dict):
            raise ValueError("Canonical row portable values are invalid")
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            target_model=str(payload["target_model"]),
            disposition=StagingDisposition(str(payload["disposition"])),
            source_identity=source_identity,
            target_identity=target_identity,
            target_scope=target_scope,
            proposed_values=proposed_values,
            references=references,
            issues=tuple(
                CanonicalIssue.from_dict(item) for item in payload.get("issues", ())
            ),
            lineage=CanonicalLineage.from_dict(dict(payload["lineage"])),
        )


@dataclass(frozen=True, slots=True)
class StagingReconciliation:
    """Complete row-count equation for one canonical evaluation."""

    total_rows: int
    candidate_rows: int
    reference_rows: int
    blocked_rows: int
    quarantined_rows: int = 0
    excluded_rows: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.candidate_rows,
            self.reference_rows,
            self.blocked_rows,
            self.quarantined_rows,
            self.excluded_rows,
        )
        if self.total_rows < 0 or any(item < 0 for item in counts):
            raise ValueError("Staging reconciliation counts cannot be negative")
        if self.total_rows != sum(counts):
            raise ValueError("Staging reconciliation does not account for every row")

    def to_portable_dict(self) -> dict[str, int]:
        """Serialize the run-wide row-count equation."""

        return {
            "total_rows": self.total_rows,
            "candidate_rows": self.candidate_rows,
            "reference_rows": self.reference_rows,
            "blocked_rows": self.blocked_rows,
            "quarantined_rows": self.quarantined_rows,
            "excluded_rows": self.excluded_rows,
        }

    @classmethod
    def from_rows(cls, rows: Iterable[CanonicalRow]) -> "StagingReconciliation":
        """Count each disposition and prove every canonical row is represented."""

        items = tuple(rows)
        counts = _count_dispositions(items)
        return cls(
            total_rows=len(items),
            candidate_rows=counts[StagingDisposition.CANDIDATE],
            reference_rows=counts[StagingDisposition.REFERENCE],
            blocked_rows=counts[StagingDisposition.BLOCKED],
            quarantined_rows=counts[StagingDisposition.QUARANTINED],
            excluded_rows=counts[StagingDisposition.EXCLUDED],
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StagingReconciliation":
        """Reconstruct and validate the run-wide reconciliation equation."""

        return cls(
            total_rows=int(payload["total_rows"]),
            candidate_rows=int(payload["candidate_rows"]),
            reference_rows=int(payload["reference_rows"]),
            blocked_rows=int(payload["blocked_rows"]),
            quarantined_rows=int(payload.get("quarantined_rows", 0)),
            excluded_rows=int(payload.get("excluded_rows", 0)),
        )


@dataclass(frozen=True, slots=True)
class StagingDatasetReconciliation:
    """Explain how one effective dataset accounts for physical source rows."""

    dataset: str
    target_model: str
    physical_dataset_id: str
    role: StagingDatasetRole
    input_rows: int
    input_rows_used: int
    output_rows: int
    lineage_links: int
    created_rows: int
    combined_rows: int
    unrepresented_rows: int
    candidate_rows: int
    reference_rows: int
    blocked_rows: int
    quarantined_rows: int = 0
    excluded_rows: int = 0

    def __post_init__(self) -> None:
        if not self.dataset or not self.target_model or not self.physical_dataset_id:
            raise ValueError("Dataset reconciliation identifiers are required")
        counts = (
            self.input_rows,
            self.input_rows_used,
            self.output_rows,
            self.lineage_links,
            self.created_rows,
            self.combined_rows,
            self.unrepresented_rows,
            self.candidate_rows,
            self.reference_rows,
            self.blocked_rows,
            self.quarantined_rows,
            self.excluded_rows,
        )
        if any(item < 0 for item in counts):
            raise ValueError("Dataset reconciliation counts cannot be negative")
        if self.input_rows_used > self.input_rows:
            raise ValueError("Used source rows cannot exceed input rows")
        if self.unrepresented_rows != self.input_rows - self.input_rows_used:
            raise ValueError("Dataset reconciliation has unexplained source rows")
        if self.output_rows and self.lineage_links < self.output_rows:
            raise ValueError("Every output row requires source lineage")
        if self.created_rows != max(self.output_rows - self.input_rows_used, 0):
            raise ValueError("Dataset created-row count is inconsistent")
        if self.combined_rows != max(self.lineage_links - self.output_rows, 0):
            raise ValueError("Dataset combined-row count is inconsistent")
        if self.output_rows != sum(
            (
                self.candidate_rows,
                self.reference_rows,
                self.blocked_rows,
                self.quarantined_rows,
                self.excluded_rows,
            )
        ):
            raise ValueError("Dataset reconciliation does not account for outputs")

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize physical-input, lineage, output, and disposition counts."""

        return {
            "dataset": self.dataset,
            "target_model": self.target_model,
            "physical_dataset_id": self.physical_dataset_id,
            "role": self.role.value,
            "input_rows": self.input_rows,
            "input_rows_used": self.input_rows_used,
            "output_rows": self.output_rows,
            "lineage_links": self.lineage_links,
            "created_rows": self.created_rows,
            "combined_rows": self.combined_rows,
            "unrepresented_rows": self.unrepresented_rows,
            "candidate_rows": self.candidate_rows,
            "reference_rows": self.reference_rows,
            "blocked_rows": self.blocked_rows,
            "quarantined_rows": self.quarantined_rows,
            "excluded_rows": self.excluded_rows,
        }

    @classmethod
    def from_rows(
        cls,
        *,
        dataset: str,
        target_model: str,
        physical_dataset_id: str,
        role: StagingDatasetRole,
        input_rows: int,
        source_rows: Iterable[int],
        lineage_links: int,
        rows: Iterable[CanonicalRow],
    ) -> "StagingDatasetReconciliation":
        """Build one dataset equation from canonical rows and lineage links."""

        items = tuple(rows)
        used = tuple(sorted(set(source_rows)))
        counts = _count_dispositions(items)
        return cls(
            dataset=dataset,
            target_model=target_model,
            physical_dataset_id=physical_dataset_id,
            role=role,
            input_rows=input_rows,
            input_rows_used=len(used),
            output_rows=len(items),
            lineage_links=lineage_links,
            created_rows=max(len(items) - len(used), 0),
            combined_rows=max(lineage_links - len(items), 0),
            unrepresented_rows=input_rows - len(used),
            candidate_rows=counts[StagingDisposition.CANDIDATE],
            reference_rows=counts[StagingDisposition.REFERENCE],
            blocked_rows=counts[StagingDisposition.BLOCKED],
            quarantined_rows=counts[StagingDisposition.QUARANTINED],
            excluded_rows=counts[StagingDisposition.EXCLUDED],
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "StagingDatasetReconciliation":
        """Reconstruct and validate one dataset reconciliation equation."""

        return cls(
            dataset=str(payload["dataset"]),
            target_model=str(payload["target_model"]),
            physical_dataset_id=str(payload["physical_dataset_id"]),
            role=StagingDatasetRole(str(payload["role"])),
            input_rows=int(payload["input_rows"]),
            input_rows_used=int(payload["input_rows_used"]),
            output_rows=int(payload["output_rows"]),
            lineage_links=int(payload["lineage_links"]),
            created_rows=int(payload["created_rows"]),
            combined_rows=int(payload["combined_rows"]),
            unrepresented_rows=int(payload["unrepresented_rows"]),
            candidate_rows=int(payload["candidate_rows"]),
            reference_rows=int(payload["reference_rows"]),
            blocked_rows=int(payload["blocked_rows"]),
            quarantined_rows=int(payload.get("quarantined_rows", 0)),
            excluded_rows=int(payload.get("excluded_rows", 0)),
        )


@dataclass(frozen=True, slots=True)
class CanonicalStagingRun:
    """Deterministic, versioned result of one full-row source evaluation.

    This is the durable Stage-E handoff: mapping compilation and preparation
    have finished, but target lookup and execution have not begun. Its hashes,
    rows, lineage, issues, reconciliations, and control totals are validated as
    one immutable unit before quality evaluation can consume it.
    """

    project_id: str
    mapping_id: str
    physical_selection_hash: str
    source_selection_hash: str
    mapping_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    datasets: tuple[StagingDatasetReconciliation, ...]
    rows: tuple[CanonicalRow, ...]
    issues: tuple[CanonicalIssue, ...]
    reconciliation: StagingReconciliation
    compiled_plan_hash: str | None = None
    control_totals: tuple[CanonicalControlTotal, ...] = ()
    evaluator_version: int = BROWSER_EVALUATOR_VERSION
    contract_version: int = STAGING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for label, value in (
            ("physical_selection_hash", self.physical_selection_hash),
            ("source_selection_hash", self.source_selection_hash),
            ("mapping_hash", self.mapping_hash),
            ("schema_hash", self.schema_hash),
        ):
            _require_hash(value, label)
        if self.derived_plan_hash is not None:
            _require_hash(self.derived_plan_hash, "derived_plan_hash")
        if self.compiled_plan_hash is None:
            raise ValueError("Current staging evidence requires a compiled plan")
        _require_hash(self.compiled_plan_hash, "compiled_plan_hash")
        if self.contract_version != STAGING_CONTRACT_VERSION:
            raise ValueError("Staging contract version is unsupported")
        if self.evaluator_version != BROWSER_EVALUATOR_VERSION:
            raise ValueError("Browser evaluator version is unsupported")
        if not self.project_id or not self.mapping_id:
            raise ValueError("Staging run must identify its project and mapping")
        expected_order = tuple(sorted(self.rows, key=_row_order))
        if self.rows != expected_order:
            raise ValueError("Canonical rows must use deterministic ordering")
        if len({item.row_id for item in self.rows}) != len(self.rows):
            raise ValueError("Canonical row identifiers must be unique")
        if self.datasets != tuple(sorted(self.datasets, key=lambda item: item.dataset)):
            raise ValueError("Dataset reconciliation must use deterministic ordering")
        if len({item.dataset for item in self.datasets}) != len(self.datasets):
            raise ValueError("Dataset reconciliation names must be unique")
        expected_controls = tuple(
            sorted(self.control_totals, key=lambda item: item.control_id)
        )
        if self.control_totals != expected_controls:
            raise ValueError("Control totals must use deterministic ordering")
        if len({item.control_id for item in self.control_totals}) != len(
            self.control_totals
        ):
            raise ValueError("Control-total identifiers must be unique")
        if self.reconciliation != StagingReconciliation.from_rows(self.rows):
            raise ValueError("Staging reconciliation does not match canonical rows")
        for index, row in enumerate(self.rows):
            _validate_row(row, self)
            assert_no_numeric_odoo_ids(
                row.to_portable_dict(),
                path=f"$.rows[{index}]",
            )
        rows_by_dataset_lists: dict[str, list[CanonicalRow]] = {
            item.dataset: [] for item in self.datasets
        }
        unexpected_datasets: set[str] = set()
        for row in self.rows:
            dataset_rows = rows_by_dataset_lists.get(row.dataset)
            if dataset_rows is None:
                unexpected_datasets.add(row.dataset)
                continue
            dataset_rows.append(row)
        if unexpected_datasets:
            raise ValueError("Canonical rows are missing dataset reconciliation")
        for item in self.datasets:
            rows = tuple(rows_by_dataset_lists[item.dataset])
            if item.output_rows != len(rows):
                raise ValueError("Dataset reconciliation has the wrong output count")
            dispositions = StagingReconciliation.from_rows(rows)
            if (
                item.candidate_rows != dispositions.candidate_rows
                or item.reference_rows != dispositions.reference_rows
                or item.blocked_rows != dispositions.blocked_rows
                or item.quarantined_rows != dispositions.quarantined_rows
                or item.excluded_rows != dispositions.excluded_rows
            ):
                raise ValueError("Dataset dispositions do not match canonical rows")
        if sum(item.output_rows for item in self.datasets) != len(self.rows):
            raise ValueError("Dataset reconciliation does not match the run")

    @property
    def content_hash(self) -> str:
        """Hash all semantic Stage-E evidence for downstream binding."""

        return "sha256:" + sha256(
            canonical_json_bytes(self.to_portable_dict(include_hash=False))
        ).hexdigest()

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Serialize the complete portable staging contract."""

        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "evaluator_version": self.evaluator_version,
            "project_id": self.project_id,
            "mapping_id": self.mapping_id,
            "physical_selection_hash": self.physical_selection_hash,
            "source_selection_hash": self.source_selection_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "derived_plan_hash": self.derived_plan_hash,
            "datasets": [item.to_portable_dict() for item in self.datasets],
            "rows": [item.to_portable_dict() for item in self.rows],
            "issues": [item.to_portable_dict() for item in self.issues],
            "reconciliation": self.reconciliation.to_portable_dict(),
        }
        payload["control_totals"] = [
            item.to_portable_dict() for item in self.control_totals
        ]
        payload["compiled_plan_hash"] = self.compiled_plan_hash
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Return the canonical staging run as canonical JSON."""

        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalStagingRun":
        """Load a run and enforce versions, reconciliation, and content hash."""

        if set(payload) != {
            "contract_version",
            "evaluator_version",
            "project_id",
            "mapping_id",
            "physical_selection_hash",
            "source_selection_hash",
            "mapping_hash",
            "schema_hash",
            "derived_plan_hash",
            "compiled_plan_hash",
            "datasets",
            "rows",
            "issues",
            "reconciliation",
            "control_totals",
            "content_hash",
        }:
            raise ValueError("Staging fields do not match the current contract")
        run = cls(
            contract_version=int(payload["contract_version"]),
            evaluator_version=int(payload["evaluator_version"]),
            project_id=str(payload["project_id"]),
            mapping_id=str(payload["mapping_id"]),
            physical_selection_hash=str(payload["physical_selection_hash"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            derived_plan_hash=(
                str(payload["derived_plan_hash"])
                if payload.get("derived_plan_hash")
                else None
            ),
            datasets=tuple(
                StagingDatasetReconciliation.from_dict(item)
                for item in payload["datasets"]
            ),
            rows=tuple(
                CanonicalRow.from_dict(item)
                for item in payload["rows"]
            ),
            issues=tuple(
                CanonicalIssue.from_dict(item) for item in payload["issues"]
            ),
            reconciliation=StagingReconciliation.from_dict(
                dict(payload["reconciliation"])
            ),
            compiled_plan_hash=(
                str(payload["compiled_plan_hash"])
                if payload.get("compiled_plan_hash")
                else None
            ),
            control_totals=tuple(
                CanonicalControlTotal.from_dict(item)
                for item in payload["control_totals"]
            ),
        )
        if payload["content_hash"] != run.content_hash:
            raise ValueError("Canonical staging content hash is invalid")
        return run

    @classmethod
    def from_json(cls, value: str) -> "CanonicalStagingRun":
        """Load and validate a canonical staging run from JSON."""

        return cls.from_dict(json.loads(value))

    @classmethod
    def from_prepared(
        cls,
        *,
        project_id: str,
        mapping_id: str,
        physical_selection_hash: str,
        source_selection_hash: str,
        mapping_hash: str,
        schema_hash: str,
        derived_plan_hash: str | None,
        plan: CompiledMigrationPlan,
        prepared: PreparedBundle,
        field_sources: Mapping[str, Mapping[str, tuple[str, ...]]],
        source_lineage: Mapping[
            tuple[str, int],
            tuple[str, tuple[int, ...]] | Mapping[str, tuple[int, ...]],
        ],
        dataset_evidence: Mapping[
            str, tuple[str, StagingDatasetRole, int]
        ],
        control_totals: tuple[CanonicalControlTotal, ...] = (),
    ) -> "CanonicalStagingRun":
        """Convert compiler/preparation output into durable Stage-E evidence.

        Each prepared record becomes a canonical row with logical references
        and source lineage. Dataset/run reconciliation and compiled-plan hash
        are assembled here so the resulting object can validate atomically.
        """

        mode_by_dataset = {
            dataset.name: dataset.target.mode for dataset in plan.datasets
        }
        target_model_by_dataset = {
            dataset.name: dataset.target.model for dataset in plan.datasets
        }
        lineage_parts = {
            key: _lineage_parts(value) for key, value in source_lineage.items()
        }
        rows = tuple(
            sorted(
                (
                    _canonical_row(
                        record,
                        mode=mode_by_dataset[record.dataset],
                        source_hash=prepared.source_hashes[record.dataset],
                        source_selection_hash=source_selection_hash,
                        mapping_hash=mapping_hash,
                        schema_hash=schema_hash,
                        derived_plan_hash=derived_plan_hash,
                        field_sources=field_sources.get(record.dataset, {}),
                        physical_dataset_id=lineage_parts[
                            (record.dataset, record.source_row)
                        ][0],
                        physical_source_rows=lineage_parts[
                            (record.dataset, record.source_row)
                        ][1],
                        physical_sources=lineage_parts[
                            (record.dataset, record.source_row)
                        ][2],
                    )
                    for record in prepared.records
                ),
                key=_row_order,
            )
        )
        rows_by_dataset: dict[str, list[CanonicalRow]] = {
            dataset: [] for dataset in dataset_evidence
        }
        for item in rows:
            if item.dataset in rows_by_dataset:
                rows_by_dataset[item.dataset].append(item)
        source_coordinates_by_dataset: dict[str, set[tuple[str, int]]] = {
            dataset: set() for dataset in dataset_evidence
        }
        lineage_links_by_dataset = dict.fromkeys(dataset_evidence, 0)
        for (dataset, _), (_, source_rows, all_sources) in lineage_parts.items():
            if dataset not in source_coordinates_by_dataset:
                continue
            source_coordinates_by_dataset[dataset].update(
                (physical_dataset_id, source_row)
                for physical_dataset_id, physical_rows in all_sources.items()
                for source_row in physical_rows
            )
            lineage_links_by_dataset[dataset] += sum(
                len(physical_rows) for physical_rows in all_sources.values()
            )
        datasets = tuple(
            sorted(
                (
                    StagingDatasetReconciliation.from_rows(
                        dataset=dataset,
                        target_model=(
                            items[0].target_model
                            if items
                            else target_model_by_dataset[dataset]
                        ),
                        physical_dataset_id=evidence[0],
                        role=evidence[1],
                        input_rows=evidence[2],
                        source_rows=range(
                            1,
                            len(source_coordinates_by_dataset[dataset]) + 1,
                        ),
                        lineage_links=lineage_links_by_dataset[dataset],
                        rows=items,
                    )
                    for dataset, evidence in dataset_evidence.items()
                    for items in (rows_by_dataset[dataset],)
                ),
                key=lambda item: item.dataset,
            )
        )
        return cls(
            project_id=project_id,
            mapping_id=mapping_id,
            physical_selection_hash=physical_selection_hash,
            source_selection_hash=source_selection_hash,
            mapping_hash=mapping_hash,
            schema_hash=schema_hash,
            derived_plan_hash=derived_plan_hash,
            datasets=datasets,
            rows=rows,
            issues=tuple(CanonicalIssue.from_issue(item) for item in prepared.issues),
            reconciliation=StagingReconciliation.from_rows(rows),
            compiled_plan_hash=plan.semantic_hash,
            control_totals=tuple(
                sorted(control_totals, key=lambda item: item.control_id)
            ),
        )


def _canonical_row(
    record: PreparedRecord,
    *,
    mode: str,
    source_hash: str,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    derived_plan_hash: str | None,
    field_sources: Mapping[str, tuple[str, ...]],
    physical_dataset_id: str,
    physical_source_rows: tuple[int, ...],
    physical_sources: Mapping[str, tuple[int, ...]] | None = None,
) -> CanonicalRow:
    disposition = (
        StagingDisposition.BLOCKED
        if record.blocked
        else (
            StagingDisposition.REFERENCE
            if mode == "reference"
            else StagingDisposition.CANDIDATE
        )
    )
    lineage = CanonicalLineage(
        source_selection_hash=source_selection_hash,
        source_hash=source_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        derived_plan_hash=derived_plan_hash,
        dataset=record.dataset,
        source_row=record.source_row,
        physical_dataset_id=physical_dataset_id,
        physical_source_rows=physical_source_rows,
        field_sources=dict(sorted(field_sources.items())),
        physical_sources=physical_sources or {},
    )
    row_id = "sha256:" + sha256(
        canonical_json_bytes(
            {
                "lineage": lineage.to_portable_dict(),
                "target_model": record.target_model,
                "source_identity": portable_value(record.source_identity),
            }
        )
    ).hexdigest()
    return CanonicalRow(
        row_id=row_id,
        dataset=record.dataset,
        source_row=record.source_row,
        target_model=record.target_model,
        disposition=disposition,
        source_identity=record.source_identity,
        target_identity=record.target_identity,
        target_scope=record.target_scope,
        proposed_values=dict(record.scalar_values),
        references=dict(record.references),
        issues=tuple(CanonicalIssue.from_issue(item) for item in record.issues),
        lineage=lineage,
    )


def canonical_row_from_prepared(
    record: PreparedRecord,
    *,
    mode: str,
    source_hash: str,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    derived_plan_hash: str | None,
    field_sources: Mapping[str, tuple[str, ...]],
    physical_dataset_id: str,
    physical_source_rows: tuple[int, ...],
    physical_sources: Mapping[str, tuple[int, ...]] | None = None,
) -> CanonicalRow:
    """Build one canonical row for either materialized or streamed staging."""

    return _canonical_row(
        record,
        mode=mode,
        source_hash=source_hash,
        source_selection_hash=source_selection_hash,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        derived_plan_hash=derived_plan_hash,
        field_sources=field_sources,
        physical_dataset_id=physical_dataset_id,
        physical_source_rows=physical_source_rows,
        physical_sources=physical_sources,
    )


def _lineage_parts(
    value: tuple[str, tuple[int, ...]] | Mapping[str, tuple[int, ...]],
) -> tuple[str, tuple[int, ...], dict[str, tuple[int, ...]]]:
    if isinstance(value, Mapping):
        sources = {
            str(dataset_id): tuple(source_rows)
            for dataset_id, source_rows in sorted(value.items())
        }
        if not sources:
            raise ValueError("Canonical source lineage is empty")
        primary = next(iter(sources))
        return primary, sources[primary], sources
    primary, source_rows = value
    return primary, source_rows, {primary: source_rows}


def _validate_row(row: CanonicalRow, run: CanonicalStagingRun) -> None:
    validate_canonical_row_bindings(
        row,
        source_selection_hash=run.source_selection_hash,
        mapping_hash=run.mapping_hash,
        schema_hash=run.schema_hash,
        derived_plan_hash=run.derived_plan_hash,
    )


def validate_canonical_row_bindings(
    row: CanonicalRow,
    *,
    source_selection_hash: str,
    mapping_hash: str,
    schema_hash: str,
    derived_plan_hash: str | None,
) -> None:
    """Validate one streamed row before it can enter publication storage."""

    _require_hash(row.row_id, "row_id")
    _require_hash(row.lineage.source_hash, "lineage.source_hash")
    if row.source_row < 1:
        raise ValueError("Canonical source rows must be positive")
    if row.dataset != row.lineage.dataset or row.source_row != row.lineage.source_row:
        raise ValueError("Canonical row and lineage coordinates do not match")
    expected = {
        "source_selection_hash": source_selection_hash,
        "mapping_hash": mapping_hash,
        "schema_hash": schema_hash,
    }
    for label, value in expected.items():
        if getattr(row.lineage, label) != value:
            raise ValueError(f"Canonical row lineage has a different {label}")
    if row.lineage.derived_plan_hash != derived_plan_hash:
        raise ValueError("Canonical row lineage has a different derived plan")
    blocking = any(item.blocking for item in row.issues)
    if row.disposition is StagingDisposition.BLOCKED and not blocking:
        raise ValueError("Blocked disposition requires a blocking row issue")
    if blocking and row.disposition not in {
        StagingDisposition.BLOCKED,
        StagingDisposition.QUARANTINED,
    }:
        raise ValueError(
            "Blocking row issues require a blocked or quarantined disposition"
        )
    assert_no_numeric_odoo_ids(row.to_portable_dict(), path="$.row")


def _row_order(row: CanonicalRow) -> tuple[str, int, str]:
    return (row.dataset, row.source_row, row.row_id)


def _require_hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical sha256 hash")
