"""Versioned, portable contracts for target-independent canonical evaluation.

These objects describe the deterministic output of applying one exact mapping
to every frozen source row.  They deliberately stop before Odoo resolution,
approval, persistence, or execution.  Numeric Odoo identifiers are forbidden
from every portable payload.
"""

from __future__ import annotations

from dataclasses import dataclass
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
)
from .profile import ProfileDocument
from .source import PreparedBundle


STAGING_CONTRACT_VERSION = 1
BROWSER_EVALUATOR_VERSION = 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")


class StagingDisposition(StrEnum):
    """One reconciled source-side outcome before target comparison."""

    CANDIDATE = "CANDIDATE"
    REFERENCE = "REFERENCE"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    EXCLUDED = "EXCLUDED"


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
        return self.severity == "error"

    def to_portable_dict(self) -> dict[str, Any]:
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
    field_sources: Mapping[str, tuple[str, ...]]

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "source_selection_hash": self.source_selection_hash,
            "source_hash": self.source_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "derived_plan_hash": self.derived_plan_hash,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "field_sources": {
                field: list(sources)
                for field, sources in sorted(self.field_sources.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalLineage":
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
            field_sources={
                str(field): tuple(str(item) for item in sources)
                for field, sources in dict(payload.get("field_sources", {})).items()
            },
        )


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    """One target-independent canonical row with typed proposed values."""

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
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            target_model=str(payload["target_model"]),
            disposition=StagingDisposition(str(payload["disposition"])),
            source_identity=tuple(payload.get("source_identity", ())),
            target_identity=tuple(payload.get("target_identity", ())),
            target_scope=tuple(payload.get("target_scope", ())),
            proposed_values=dict(payload.get("proposed_values", {})),
            references=dict(payload.get("references", {})),
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
        items = tuple(rows)
        return cls(
            total_rows=len(items),
            candidate_rows=sum(
                item.disposition is StagingDisposition.CANDIDATE for item in items
            ),
            reference_rows=sum(
                item.disposition is StagingDisposition.REFERENCE for item in items
            ),
            blocked_rows=sum(
                item.disposition is StagingDisposition.BLOCKED for item in items
            ),
            quarantined_rows=sum(
                item.disposition is StagingDisposition.QUARANTINED for item in items
            ),
            excluded_rows=sum(
                item.disposition is StagingDisposition.EXCLUDED for item in items
            ),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StagingReconciliation":
        return cls(
            total_rows=int(payload["total_rows"]),
            candidate_rows=int(payload["candidate_rows"]),
            reference_rows=int(payload["reference_rows"]),
            blocked_rows=int(payload["blocked_rows"]),
            quarantined_rows=int(payload.get("quarantined_rows", 0)),
            excluded_rows=int(payload.get("excluded_rows", 0)),
        )


@dataclass(frozen=True, slots=True)
class CanonicalStagingRun:
    """Deterministic, versioned result of one full-row source evaluation."""

    project_id: str
    mapping_id: str
    physical_selection_hash: str
    source_selection_hash: str
    mapping_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    rows: tuple[CanonicalRow, ...]
    issues: tuple[CanonicalIssue, ...]
    reconciliation: StagingReconciliation
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
        if self.reconciliation != StagingReconciliation.from_rows(self.rows):
            raise ValueError("Staging reconciliation does not match canonical rows")
        for row in self.rows:
            _validate_row(row, self)
        assert_no_numeric_odoo_ids(self.to_portable_dict(include_hash=False))

    @property
    def content_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(self.to_portable_dict(include_hash=False))
        ).hexdigest()

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
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
            "rows": [item.to_portable_dict() for item in self.rows],
            "issues": [item.to_portable_dict() for item in self.issues],
            "reconciliation": self.reconciliation.to_portable_dict(),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalStagingRun":
        run = cls(
            contract_version=int(payload.get("contract_version", 0)),
            evaluator_version=int(payload.get("evaluator_version", 0)),
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
            rows=tuple(
                CanonicalRow.from_dict(item)
                for item in payload.get("rows", ())
            ),
            issues=tuple(
                CanonicalIssue.from_dict(item) for item in payload.get("issues", ())
            ),
            reconciliation=StagingReconciliation.from_dict(
                dict(payload["reconciliation"])
            ),
        )
        if payload.get("content_hash") != run.content_hash:
            raise ValueError("Canonical staging content hash is invalid")
        return run

    @classmethod
    def from_json(cls, value: str) -> "CanonicalStagingRun":
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
        profile: ProfileDocument,
        prepared: PreparedBundle,
        field_sources: Mapping[str, Mapping[str, tuple[str, ...]]],
    ) -> "CanonicalStagingRun":
        mode_by_dataset = {
            dataset.name: dataset.target.mode for dataset in profile.datasets
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
                    )
                    for record in prepared.records
                ),
                key=_row_order,
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
            rows=rows,
            issues=tuple(CanonicalIssue.from_issue(item) for item in prepared.issues),
            reconciliation=StagingReconciliation.from_rows(rows),
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
        field_sources=dict(sorted(field_sources.items())),
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


def _validate_row(row: CanonicalRow, run: CanonicalStagingRun) -> None:
    _require_hash(row.row_id, "row_id")
    _require_hash(row.lineage.source_hash, "lineage.source_hash")
    if row.source_row < 1:
        raise ValueError("Canonical source rows must be positive")
    if row.dataset != row.lineage.dataset or row.source_row != row.lineage.source_row:
        raise ValueError("Canonical row and lineage coordinates do not match")
    for label in ("source_selection_hash", "mapping_hash", "schema_hash"):
        if getattr(row.lineage, label) != getattr(run, label):
            raise ValueError(f"Canonical row lineage has a different {label}")
    if row.lineage.derived_plan_hash != run.derived_plan_hash:
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


def _row_order(row: CanonicalRow) -> tuple[str, int, str]:
    return (row.dataset, row.source_row, row.row_id)


def _require_hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical sha256 hash")
