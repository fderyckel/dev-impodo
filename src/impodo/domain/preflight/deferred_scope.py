"""Plan a reviewed set-aside scope from saved preflight evidence.

The functions in this module are pure.  They do not read Odoo, reopen source
artifacts, mutate preparation evidence, or publish an execution snapshot.
They turn already verified comparison rows and incoming-row dependency facts
into the bounded preview needed before a data manager accepts a reduced load
scope.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import re
from typing import Mapping, Protocol

from impodo.domain.shared.access import ActorIdentity
from impodo.domain.shared.models import (
    Classification,
    LogicalReference,
    PreflightResult,
    PreparedRecord,
    ReferenceResolution,
    assert_no_numeric_odoo_ids,
    canonical_json_bytes,
    portable_value,
    restore_portable_value,
)
from impodo.domain.target_numeric_precision import unrepresentable_decimal


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_WRITE_DISPOSITIONS = frozenset(
    {Classification.CREATE.value, Classification.UPDATE.value}
)
_PROBLEM_DISPOSITIONS = frozenset(
    {Classification.BLOCKED.value, Classification.AMBIGUOUS.value}
)
_INCOMING_RESOLUTION_STATUSES = frozenset(
    {"RESOLVED_INCOMING", "BLOCKED_BY_DEPENDENCY"}
)


class DeferredScopeEvidenceError(ValueError):
    """Raised when saved evidence cannot prove one safe reduced scope."""


class ExecutionRowEvidence(Protocol):
    """The portable execution-row fields needed by deferred-scope planning."""

    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    source_identity: tuple[object, ...]
    target_model: str
    disposition: str


class RelationshipBlockerEvidence(Protocol):
    """The saved schedule-blocker fields needed by issue projection."""

    row_id: str
    code: str
    field: str


class DeferredIssueScope(StrEnum):
    """Whether an issue belongs to one row or invalidates the complete run."""

    ROW = "ROW"
    RUN = "RUN"


@dataclass(frozen=True, slots=True)
class DeferredIssue:
    """One immutable problem that may be selected as a set-aside root."""

    issue_id: str
    code: str
    scope: DeferredIssueScope
    row_id: str = ""
    field: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if (
            _SHA256.fullmatch(self.issue_id) is None
            or not self.code
            or (self.scope is DeferredIssueScope.ROW and not self.row_id)
            or (self.scope is DeferredIssueScope.RUN and self.row_id)
        ):
            raise ValueError("Deferred issue evidence is invalid")

    def portable_dict(self) -> dict[str, str]:
        return {
            "issue_id": self.issue_id,
            "code": self.code,
            "scope": self.scope.value,
            "row_id": self.row_id,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class DeferredDependencyFact:
    """One verified incoming-row dependency used for closure.

    ``dependency_row_id`` is the referenced parent.  If that parent is set
    aside, the owner must also be set aside.  ``keeps_group_together`` adds the
    reverse direction for an identity or scope link: setting the owner aside
    also sets its parent aside, which then reaches sibling dependants.
    """

    dependency_row_id: str
    owner_row_id: str
    field: str
    keeps_group_together: bool

    def __post_init__(self) -> None:
        if not self.dependency_row_id or not self.owner_row_id or not self.field:
            raise ValueError("Deferred dependency evidence is invalid")

    def portable_dict(self) -> dict[str, object]:
        return {
            "dependency_row_id": self.dependency_row_id,
            "owner_row_id": self.owner_row_id,
            "field": self.field,
            "keeps_group_together": self.keeps_group_together,
        }


@dataclass(frozen=True, slots=True)
class DeferredIssueGroup:
    """The complete record closure caused by one selected root issue."""

    group_id: str
    issue_id: str
    root_row_id: str
    row_ids: tuple[str, ...]
    counts_by_dataset: tuple[tuple[str, int], ...]
    write_count: int

    def portable_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "issue_id": self.issue_id,
            "root_row_id": self.root_row_id,
            "row_ids": list(self.row_ids),
            "counts_by_dataset": dict(self.counts_by_dataset),
            "write_count": self.write_count,
        }


@dataclass(frozen=True, slots=True)
class DeferredOmittedRow:
    """One prepared record omitted directly or through dependency closure."""

    row_id: str
    dataset: str
    source_row: int
    source_trace_id: str
    disposition: str
    direct_issue_ids: tuple[str, ...]
    inherited_issue_ids: tuple[str, ...]

    @property
    def direct(self) -> bool:
        return bool(self.direct_issue_ids)

    def portable_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "source_trace_id": self.source_trace_id,
            "disposition": self.disposition,
            "direct_issue_ids": list(self.direct_issue_ids),
            "inherited_issue_ids": list(self.inherited_issue_ids),
        }


@dataclass(frozen=True, slots=True)
class DeferredScopePreview:
    """Portable local preview bound to one saved Odoo comparison."""

    comparison_id: str
    comparison_hash: str
    execution_snapshot_hash: str
    selected_issue_ids: tuple[str, ...]
    groups: tuple[DeferredIssueGroup, ...]
    omitted_rows: tuple[DeferredOmittedRow, ...]
    counts_by_dataset: tuple[tuple[str, int], ...]
    prepared_record_count: int
    already_set_aside_count: int
    original_write_count: int
    omitted_write_count: int
    remaining_write_count: int
    remaining_problem_record_count: int
    remaining_run_issue_count: int
    contract_version: int = 1

    @property
    def newly_set_aside_count(self) -> int:
        return len(self.omitted_rows)

    @property
    def ready_with_records_set_aside(self) -> bool:
        return bool(
            self.omitted_rows
            and self.remaining_write_count
            and not self.remaining_problem_record_count
            and not self.remaining_run_issue_count
        )

    @property
    def semantic_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(self.portable_dict(include_hash=False))
        ).hexdigest()

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": self.contract_version,
            "comparison_id": self.comparison_id,
            "comparison_hash": self.comparison_hash,
            "execution_snapshot_hash": self.execution_snapshot_hash,
            "selected_issue_ids": list(self.selected_issue_ids),
            "groups": [item.portable_dict() for item in self.groups],
            "omitted_rows": [item.portable_dict() for item in self.omitted_rows],
            "counts_by_dataset": dict(self.counts_by_dataset),
            "prepared_record_count": self.prepared_record_count,
            "already_set_aside_count": self.already_set_aside_count,
            "newly_set_aside_count": self.newly_set_aside_count,
            "original_write_count": self.original_write_count,
            "omitted_write_count": self.omitted_write_count,
            "remaining_write_count": self.remaining_write_count,
            "remaining_problem_record_count": self.remaining_problem_record_count,
            "remaining_run_issue_count": self.remaining_run_issue_count,
            "ready_with_records_set_aside": self.ready_with_records_set_aside,
        }
        if include_hash:
            payload["semantic_hash"] = self.semantic_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    def to_json(self) -> str:
        """Return the canonical portable preview for bounded reuse."""

        return canonical_json_bytes(self.portable_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "DeferredScopePreview":
        """Restore a preview and reject changed counts, rows, or bindings."""

        try:
            payload = json.loads(value)
            groups = tuple(
                DeferredIssueGroup(
                    group_id=str(item["group_id"]),
                    issue_id=str(item["issue_id"]),
                    root_row_id=str(item["root_row_id"]),
                    row_ids=tuple(str(row_id) for row_id in item["row_ids"]),
                    counts_by_dataset=tuple(
                        sorted(
                            (str(dataset), int(count))
                            for dataset, count in dict(
                                item["counts_by_dataset"]
                            ).items()
                        )
                    ),
                    write_count=int(item["write_count"]),
                )
                for item in payload["groups"]
            )
            omitted_rows = tuple(
                DeferredOmittedRow(
                    row_id=str(item["row_id"]),
                    dataset=str(item["dataset"]),
                    source_row=int(item["source_row"]),
                    source_trace_id=str(item["source_trace_id"]),
                    disposition=str(item["disposition"]),
                    direct_issue_ids=tuple(
                        str(issue_id) for issue_id in item["direct_issue_ids"]
                    ),
                    inherited_issue_ids=tuple(
                        str(issue_id) for issue_id in item["inherited_issue_ids"]
                    ),
                )
                for item in payload["omitted_rows"]
            )
            preview = cls(
                comparison_id=str(payload["comparison_id"]),
                comparison_hash=str(payload["comparison_hash"]),
                execution_snapshot_hash=str(payload["execution_snapshot_hash"]),
                selected_issue_ids=tuple(
                    str(item) for item in payload["selected_issue_ids"]
                ),
                groups=groups,
                omitted_rows=omitted_rows,
                counts_by_dataset=tuple(
                    sorted(
                        (str(dataset), int(count))
                        for dataset, count in dict(
                            payload["counts_by_dataset"]
                        ).items()
                    )
                ),
                prepared_record_count=int(payload["prepared_record_count"]),
                already_set_aside_count=int(payload["already_set_aside_count"]),
                original_write_count=int(payload["original_write_count"]),
                omitted_write_count=int(payload["omitted_write_count"]),
                remaining_write_count=int(payload["remaining_write_count"]),
                remaining_problem_record_count=int(
                    payload["remaining_problem_record_count"]
                ),
                remaining_run_issue_count=int(payload["remaining_run_issue_count"]),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DeferredScopeEvidenceError(
                "Stored deferred-scope preview is invalid"
            ) from error
        if (
            payload.get("semantic_hash") != preview.semantic_hash
            or int(payload.get("newly_set_aside_count", -1))
            != preview.newly_set_aside_count
            or bool(payload.get("ready_with_records_set_aside"))
            != preview.ready_with_records_set_aside
        ):
            raise DeferredScopeEvidenceError(
                "Stored deferred-scope preview hash is invalid"
            )
        _validate_preview_shape(preview)
        return preview


@dataclass(frozen=True, slots=True)
class DeferredScopeDecision:
    """One explicit acceptance bound to a preview and reduced snapshot."""

    workspace_id: str
    comparison_id: str
    comparison_hash: str
    full_execution_snapshot_hash: str
    preview_hash: str
    reduced_execution_snapshot_hash: str
    selected_issue_ids: tuple[str, ...]
    omitted_row_ids: tuple[str, ...]
    accepted_by: ActorIdentity
    accepted_at: datetime
    contract_version: int = 1

    def __post_init__(self) -> None:
        hashes = (
            self.comparison_hash,
            self.full_execution_snapshot_hash,
            self.preview_hash,
            self.reduced_execution_snapshot_hash,
        )
        if (
            not self.workspace_id
            or not self.comparison_id
            or any(_SHA256.fullmatch(item) is None for item in hashes)
            or not self.selected_issue_ids
            or self.selected_issue_ids != tuple(sorted(set(self.selected_issue_ids)))
            or not self.omitted_row_ids
            or self.omitted_row_ids != tuple(sorted(set(self.omitted_row_ids)))
            or self.accepted_at.tzinfo is None
            or self.contract_version != 1
        ):
            raise DeferredScopeEvidenceError("Deferred-scope decision is invalid")

    @classmethod
    def accept(
        cls,
        *,
        workspace_id: str,
        preview: DeferredScopePreview,
        reduced_execution_snapshot_hash: str,
        accepted_by: ActorIdentity,
        accepted_at: datetime,
    ) -> "DeferredScopeDecision":
        """Create a decision only for a safe, non-empty reduced write set."""

        _validate_preview_shape(preview)
        if not preview.ready_with_records_set_aside:
            raise DeferredScopeEvidenceError(
                "The selected groups do not leave one safe load scope"
            )
        return cls(
            workspace_id=workspace_id,
            comparison_id=preview.comparison_id,
            comparison_hash=preview.comparison_hash,
            full_execution_snapshot_hash=preview.execution_snapshot_hash,
            preview_hash=preview.semantic_hash,
            reduced_execution_snapshot_hash=reduced_execution_snapshot_hash,
            selected_issue_ids=preview.selected_issue_ids,
            omitted_row_ids=tuple(sorted(row.row_id for row in preview.omitted_rows)),
            accepted_by=accepted_by,
            accepted_at=accepted_at,
        )

    @property
    def semantic_hash(self) -> str:
        return "sha256:" + sha256(
            canonical_json_bytes(self.portable_dict(include_hash=False))
        ).hexdigest()

    def portable_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "comparison_id": self.comparison_id,
            "comparison_hash": self.comparison_hash,
            "full_execution_snapshot_hash": self.full_execution_snapshot_hash,
            "preview_hash": self.preview_hash,
            "reduced_execution_snapshot_hash": (
                self.reduced_execution_snapshot_hash
            ),
            "selected_issue_ids": list(self.selected_issue_ids),
            "omitted_row_ids": list(self.omitted_row_ids),
            "accepted_by": {
                "issuer": self.accepted_by.issuer,
                "subject_id": self.accepted_by.subject_id,
                "display_name": self.accepted_by.display_name,
            },
            "accepted_at": self.accepted_at.isoformat(),
        }
        if include_hash:
            payload["semantic_hash"] = self.semantic_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    def to_json(self) -> str:
        return canonical_json_bytes(self.portable_dict()).decode("utf-8")

    @classmethod
    def from_json(cls, value: str) -> "DeferredScopeDecision":
        """Restore one accepted decision and verify its semantic hash."""

        try:
            payload = json.loads(value)
            accepted_by = dict(payload["accepted_by"])
            decision = cls(
                workspace_id=str(payload["workspace_id"]),
                comparison_id=str(payload["comparison_id"]),
                comparison_hash=str(payload["comparison_hash"]),
                full_execution_snapshot_hash=str(
                    payload["full_execution_snapshot_hash"]
                ),
                preview_hash=str(payload["preview_hash"]),
                reduced_execution_snapshot_hash=str(
                    payload["reduced_execution_snapshot_hash"]
                ),
                selected_issue_ids=tuple(
                    str(item) for item in payload["selected_issue_ids"]
                ),
                omitted_row_ids=tuple(
                    str(item) for item in payload["omitted_row_ids"]
                ),
                accepted_by=ActorIdentity(
                    issuer=str(accepted_by["issuer"]),
                    subject_id=str(accepted_by["subject_id"]),
                    display_name=str(accepted_by["display_name"]),
                ),
                accepted_at=datetime.fromisoformat(str(payload["accepted_at"])),
                contract_version=int(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise DeferredScopeEvidenceError(
                "Stored deferred-scope decision is invalid"
            ) from error
        if payload.get("semantic_hash") != decision.semantic_hash:
            raise DeferredScopeEvidenceError(
                "Stored deferred-scope decision hash is invalid"
            )
        return decision


def preflight_deferred_issues(
    comparison_id: str,
    rows: Sequence[ExecutionRowEvidence],
    result: PreflightResult,
    *,
    relationship_blockers: Sequence[RelationshipBlockerEvidence] = (),
) -> tuple[DeferredIssue, ...]:
    """Project every saved blocking comparison fact into selectable evidence."""

    if not comparison_id:
        raise DeferredScopeEvidenceError("Comparison identity is missing")
    row_by_trace, row_by_coordinate = _row_indexes(rows)
    issues: dict[str, DeferredIssue] = {}
    matched_row_ids: set[str] = set()
    represented_blockers: set[tuple[str, str, int | None, str]] = set()

    for decision in result.decisions:
        row = _decision_row(decision, row_by_trace, row_by_coordinate)
        if row.row_id in matched_row_ids:
            raise DeferredScopeEvidenceError(
                "More than one comparison decision names the same row"
            )
        matched_row_ids.add(row.row_id)
        blocking = tuple(item for item in decision.issues if item.blocking)
        if decision.classification is Classification.AMBIGUOUS and not blocking:
            blocking = (
                _SyntheticIssue(
                    code="TARGET_IDENTITY_AMBIGUOUS",
                    field="",
                    message="The prepared identity matches more than one Odoo record.",
                ),
            )
        if decision.classification is Classification.BLOCKED and not blocking:
            raise DeferredScopeEvidenceError(
                "A blocked comparison row has no saved root issue"
            )
        for issue in blocking:
            represented_blockers.add(_issue_signature(issue))
            projected = _deferred_issue(
                comparison_id,
                str(issue.code),
                DeferredIssueScope.ROW,
                row_id=row.row_id,
                field=str(issue.field or ""),
                message=str(issue.message),
            )
            issues[projected.issue_id] = projected

    if matched_row_ids != {row.row_id for row in rows}:
        raise DeferredScopeEvidenceError(
            "Comparison decision accounting is incomplete"
        )

    for issue in result.issues:
        if not issue.blocking or _issue_signature(issue) in represented_blockers:
            continue
        projected = _deferred_issue(
            comparison_id,
            issue.code,
            DeferredIssueScope.RUN,
            field=str(issue.field or ""),
            message=issue.message,
        )
        issues[projected.issue_id] = projected

    row_by_id = {row.row_id: row for row in rows}
    for blocker in relationship_blockers:
        if blocker.row_id not in row_by_id:
            raise DeferredScopeEvidenceError(
                "A relationship blocker names an unknown comparison row"
            )
        projected = _deferred_issue(
            comparison_id,
            f"RELATIONSHIP_{blocker.code}",
            DeferredIssueScope.ROW,
            row_id=blocker.row_id,
            field=blocker.field,
            message="The reviewed relationship schedule cannot load this record safely.",
        )
        issues[projected.issue_id] = projected

    covered = {
        item.row_id
        for item in issues.values()
        if item.scope is DeferredIssueScope.ROW
    }
    missing = sorted(
        row.row_id
        for row in rows
        if row.disposition in _PROBLEM_DISPOSITIONS and row.row_id not in covered
    )
    if missing:
        raise DeferredScopeEvidenceError(
            "Comparison problem rows are missing root-cause evidence"
        )
    return tuple(sorted(issues.values(), key=lambda item: item.issue_id))


def portable_preflight_deferred_issues(
    comparison_id: str,
    rows: Sequence[ExecutionRowEvidence],
    manifest: Mapping[str, object],
    *,
    relationship_blockers: Sequence[RelationshipBlockerEvidence] = (),
) -> tuple[DeferredIssue, ...]:
    """Project issues from the verified portable comparison manifest."""

    try:
        decisions = tuple(
            _PortableDecision(
                dataset=str(item["dataset"]),
                source_row=int(item["source_row"]),
                source_trace_id=str(item.get("source_trace_id", "")),
                classification=Classification(str(item["classification"])),
                issues=tuple(
                    _PortableIssue(
                        code=str(issue["code"]),
                        dataset=str(issue.get("dataset") or ""),
                        row=(
                            int(issue["row"])
                            if issue.get("row") is not None
                            else None
                        ),
                        field=str(issue.get("field") or ""),
                        message=str(issue["message"]),
                        severity=str(issue["severity"]),
                    )
                    for issue in item.get("issues", ())
                ),
            )
            for raw in manifest["decisions"]
            for item in (dict(raw),)
        )
        source_issues = tuple(
            _PortableIssue(
                code=str(item["code"]),
                dataset=str(item.get("dataset") or ""),
                row=(int(item["row"]) if item.get("row") is not None else None),
                field=str(item.get("field") or ""),
                message=str(item["message"]),
                severity=str(item["severity"]),
            )
            for raw in manifest.get("source_issues", ())
            for item in (dict(raw),)
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DeferredScopeEvidenceError(
            "Saved comparison issue evidence is invalid"
        ) from error
    return preflight_deferred_issues(
        comparison_id,
        rows,
        _PortableResult(decisions=decisions, issues=source_issues),
        relationship_blockers=relationship_blockers,
    )


def portable_reference_resolutions(
    manifest: Mapping[str, object],
) -> tuple[ReferenceResolution, ...]:
    """Restore only the reference outcomes needed by dependency closure."""

    try:
        resolutions = []
        for raw in manifest["reference_resolutions"]:
            item = dict(raw)
            reference = restore_portable_value(item["reference"])
            if not isinstance(reference, LogicalReference):
                raise ValueError("Reference resolution is not logical")
            resolutions.append(
                ReferenceResolution(
                    dataset=str(item["dataset"]),
                    field=str(item["field"]),
                    reference=reference,
                    status=str(item["status"]),
                    match_count=int(item["match_count"]),
                    target_binding_hash=str(
                        item.get("target_binding_hash") or ""
                    ),
                    affected_count=int(item.get("affected_count", 1)),
                )
            )
    except (KeyError, TypeError, ValueError) as error:
        raise DeferredScopeEvidenceError(
            "Saved relationship-resolution evidence is invalid"
        ) from error
    return tuple(resolutions)


def exact_numeric_precision_issues(
    comparison_id: str,
    rows: Sequence[ExecutionRowEvidence],
    datasets: Sequence[object],
) -> tuple[DeferredIssue, ...]:
    """Find exact intended writes that captured Odoo precision would change."""

    digits_by_dataset: dict[str, dict[str, tuple[int, int]]] = {}
    for dataset in datasets:
        name = str(getattr(dataset, "dataset", ""))
        if not name or name in digits_by_dataset:
            raise DeferredScopeEvidenceError(
                "Execution precision evidence has duplicate datasets"
            )
        digits_by_dataset[name] = {
            str(field): (int(digits[0]), int(digits[1]))
            for field, digits in getattr(dataset, "field_digits", ())
        }

    issues = []
    for row in rows:
        if row.disposition not in _WRITE_DISPOSITIONS:
            continue
        digits_by_field = digits_by_dataset.get(row.dataset)
        if digits_by_field is None:
            raise DeferredScopeEvidenceError(
                "Execution precision evidence names an unknown dataset"
            )
        for intent in getattr(row, "fields", ()):
            field = str(getattr(intent, "field", ""))
            digits = digits_by_field.get(field)
            if (
                digits is None
                or getattr(intent, "action", "") != "SET_VALUE"
                or unrepresentable_decimal(getattr(intent, "value", None), digits)
                is None
            ):
                continue
            issue = _deferred_issue(
                comparison_id,
                "TARGET_NUMERIC_PRECISION_LOSS",
                DeferredIssueScope.ROW,
                row_id=row.row_id,
                field=field,
                message=(
                    f"{row.target_model}.{field} cannot represent this intended "
                    f"write exactly at Odoo precision ({digits[0]}, {digits[1]})."
                ),
            )
            issues.append(issue)
    return tuple(sorted(issues, key=lambda item: item.issue_id))


def prepared_dependency_facts(
    rows: Sequence[ExecutionRowEvidence],
    records: Sequence[PreparedRecord],
    resolutions: Sequence[ReferenceResolution],
) -> tuple[DeferredDependencyFact, ...]:
    """Build exact row dependencies without reopening source or Odoo evidence."""

    row_by_record = _record_row_index(rows, records)
    source_index: dict[
        tuple[str, bytes], list[ExecutionRowEvidence]
    ] = defaultdict(list)
    for record in records:
        source_index[
            (
                record.dataset,
                canonical_json_bytes(portable_value(record.source_identity)),
            )
        ].append(row_by_record[_record_coordinate(record)])

    resolution_statuses: dict[tuple[str, bytes], set[str]] = defaultdict(set)
    for item in resolutions:
        resolution_statuses[
            (
                item.dataset,
                canonical_json_bytes(portable_value(item.reference)),
            )
        ].add(item.status)

    facts: dict[tuple[str, str, str, bool], DeferredDependencyFact] = {}
    for record in records:
        owner = row_by_record[_record_coordinate(record)]
        contexts = (
            *(
                (reference, "Odoo match", True)
                for value in record.target_identity
                for reference in _logical_references(value)
            ),
            *(
                (reference, "Odoo match scope", True)
                for value in record.target_scope
                for reference in _logical_references(value)
            ),
            *(
                (reference, field, False)
                for field, value in record.references.items()
                for reference in _logical_references(value)
            ),
        )
        for reference, field, keeps_group_together in contexts:
            incoming = _incoming_reference(
                record.dataset,
                reference,
                resolution_statuses,
            )
            if incoming is None:
                continue
            dataset, key = incoming
            matches = source_index.get(
                (dataset, canonical_json_bytes(portable_value(key))),
                (),
            )
            if len(matches) != 1:
                raise DeferredScopeEvidenceError(
                    "An incoming dependency is missing or ambiguous in saved evidence"
                )
            dependency = matches[0]
            fact = DeferredDependencyFact(
                dependency_row_id=dependency.row_id,
                owner_row_id=owner.row_id,
                field=field,
                keeps_group_together=keeps_group_together,
            )
            facts[
                (
                    fact.dependency_row_id,
                    fact.owner_row_id,
                    fact.field,
                    fact.keeps_group_together,
                )
            ] = fact

    return tuple(
        sorted(
            facts.values(),
            key=lambda item: (
                item.dependency_row_id,
                item.owner_row_id,
                item.field,
                item.keeps_group_together,
            ),
        )
    )


def preview_deferred_scope(
    *,
    comparison_id: str,
    comparison_hash: str,
    execution_snapshot_hash: str,
    rows: Sequence[ExecutionRowEvidence],
    issues: Sequence[DeferredIssue],
    dependencies: Sequence[DeferredDependencyFact],
    selected_issue_ids: Iterable[str],
    already_set_aside_count: int = 0,
) -> DeferredScopePreview:
    """Calculate one deterministic reduced-scope preview from local evidence."""

    if (
        not comparison_id
        or _SHA256.fullmatch(comparison_hash) is None
        or _SHA256.fullmatch(execution_snapshot_hash) is None
        or already_set_aside_count < 0
    ):
        raise DeferredScopeEvidenceError("Deferred scope binding is invalid")

    row_by_id = _unique_rows(rows)
    issue_by_id = _unique_issues(issues, row_by_id)
    selected = tuple(sorted(set(selected_issue_ids)))
    if not selected:
        raise DeferredScopeEvidenceError("Select at least one record-group issue")
    unknown = tuple(item for item in selected if item not in issue_by_id)
    if unknown:
        raise DeferredScopeEvidenceError("Selected set-aside issues are not current")
    if any(
        issue_by_id[item].scope is DeferredIssueScope.RUN for item in selected
    ):
        raise DeferredScopeEvidenceError(
            "A run-wide failure cannot be set aside as a record group"
        )

    forward: dict[str, set[str]] = defaultdict(set)
    reverse_group: dict[str, set[str]] = defaultdict(set)
    for fact in dependencies:
        if (
            fact.dependency_row_id not in row_by_id
            or fact.owner_row_id not in row_by_id
        ):
            raise DeferredScopeEvidenceError(
                "Deferred dependency evidence names an unknown comparison row"
            )
        forward[fact.dependency_row_id].add(fact.owner_row_id)
        if fact.keeps_group_together:
            reverse_group[fact.owner_row_id].add(fact.dependency_row_id)

    closure_by_issue: dict[str, frozenset[str]] = {}
    groups = []
    for issue_id in selected:
        issue = issue_by_id[issue_id]
        closure = _dependency_closure(
            issue.row_id,
            forward=forward,
            reverse_group=reverse_group,
        )
        closure_by_issue[issue_id] = closure
        closure_rows = tuple(
            sorted((row_by_id[item] for item in closure), key=_row_sort_key)
        )
        counts = _counts_by_dataset(closure_rows)
        group_payload = {
            "comparison_id": comparison_id,
            "issue_id": issue_id,
            "row_ids": [row.row_id for row in closure_rows],
        }
        groups.append(
            DeferredIssueGroup(
                group_id="sha256:"
                + sha256(canonical_json_bytes(group_payload)).hexdigest(),
                issue_id=issue_id,
                root_row_id=issue.row_id,
                row_ids=tuple(row.row_id for row in closure_rows),
                counts_by_dataset=counts,
                write_count=sum(
                    row.disposition in _WRITE_DISPOSITIONS for row in closure_rows
                ),
            )
        )

    omitted_ids = frozenset().union(*closure_by_issue.values())
    omitted_rows = []
    for row in sorted((row_by_id[item] for item in omitted_ids), key=_row_sort_key):
        causes = tuple(
            issue_id
            for issue_id in selected
            if row.row_id in closure_by_issue[issue_id]
        )
        direct = tuple(
            issue_id for issue_id in causes if issue_by_id[issue_id].row_id == row.row_id
        )
        inherited = tuple(issue_id for issue_id in causes if issue_id not in direct)
        omitted_rows.append(
            DeferredOmittedRow(
                row_id=row.row_id,
                dataset=row.dataset,
                source_row=row.source_row,
                source_trace_id=row.source_trace_id,
                disposition=row.disposition,
                direct_issue_ids=direct,
                inherited_issue_ids=inherited,
            )
        )

    original_write_count = sum(
        row.disposition in _WRITE_DISPOSITIONS for row in row_by_id.values()
    )
    omitted_write_count = sum(
        row.disposition in _WRITE_DISPOSITIONS for row in omitted_rows
    )
    surviving_problem_rows = {
        issue.row_id
        for issue in issues
        if issue.scope is DeferredIssueScope.ROW and issue.row_id not in omitted_ids
    }
    surviving_run_issues = sum(
        issue.scope is DeferredIssueScope.RUN for issue in issues
    )
    preview = DeferredScopePreview(
        comparison_id=comparison_id,
        comparison_hash=comparison_hash,
        execution_snapshot_hash=execution_snapshot_hash,
        selected_issue_ids=selected,
        groups=tuple(sorted(groups, key=lambda item: item.group_id)),
        omitted_rows=tuple(omitted_rows),
        counts_by_dataset=_counts_by_dataset(tuple(omitted_rows)),
        prepared_record_count=len(row_by_id) + already_set_aside_count,
        already_set_aside_count=already_set_aside_count,
        original_write_count=original_write_count,
        omitted_write_count=omitted_write_count,
        remaining_write_count=original_write_count - omitted_write_count,
        remaining_problem_record_count=len(surviving_problem_rows),
        remaining_run_issue_count=surviving_run_issues,
    )
    _validate_preview_shape(preview)
    return preview


@dataclass(frozen=True, slots=True)
class _SyntheticIssue:
    code: str
    field: str
    message: str

    @property
    def blocking(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class _PortableIssue:
    code: str
    dataset: str
    row: int | None
    field: str
    message: str
    severity: str

    @property
    def blocking(self) -> bool:
        return self.severity == "error"


@dataclass(frozen=True, slots=True)
class _PortableDecision:
    dataset: str
    source_row: int
    source_trace_id: str
    classification: Classification
    issues: tuple[_PortableIssue, ...]


@dataclass(frozen=True, slots=True)
class _PortableResult:
    decisions: tuple[_PortableDecision, ...]
    issues: tuple[_PortableIssue, ...]


def _deferred_issue(
    comparison_id: str,
    code: str,
    scope: DeferredIssueScope,
    *,
    row_id: str = "",
    field: str = "",
    message: str = "",
) -> DeferredIssue:
    payload = {
        "comparison_id": comparison_id,
        "code": code,
        "scope": scope.value,
        "row_id": row_id,
        "field": field,
    }
    return DeferredIssue(
        issue_id="sha256:" + sha256(canonical_json_bytes(payload)).hexdigest(),
        code=code,
        scope=scope,
        row_id=row_id,
        field=field,
        message=message,
    )


def _issue_signature(issue: object) -> tuple[str, str, int | None, str]:
    """Identify one engine blocker independent of aggregation counts."""

    row = getattr(issue, "row", None)
    return (
        str(getattr(issue, "code", "")),
        str(getattr(issue, "dataset", "") or ""),
        int(row) if row is not None else None,
        str(getattr(issue, "field", "") or ""),
    )


def _row_indexes(
    rows: Sequence[ExecutionRowEvidence],
) -> tuple[
    dict[tuple[str, str], ExecutionRowEvidence],
    dict[tuple[str, int], tuple[ExecutionRowEvidence, ...]],
]:
    trace: dict[tuple[str, str], ExecutionRowEvidence] = {}
    coordinates: dict[tuple[str, int], list[ExecutionRowEvidence]] = defaultdict(list)
    for row in rows:
        if row.source_trace_id:
            key = (row.dataset, row.source_trace_id)
            if key in trace:
                raise DeferredScopeEvidenceError(
                    "Comparison source traces are not unique"
                )
            trace[key] = row
        coordinates[(row.dataset, row.source_row)].append(row)
    return trace, {key: tuple(value) for key, value in coordinates.items()}


def _decision_row(
    decision,
    row_by_trace,
    row_by_coordinate,
) -> ExecutionRowEvidence:
    if decision.source_trace_id:
        row = row_by_trace.get((decision.dataset, decision.source_trace_id))
        if row is not None:
            return row
    matches = row_by_coordinate.get((decision.dataset, decision.source_row), ())
    if len(matches) != 1:
        raise DeferredScopeEvidenceError(
            "A comparison decision cannot be matched to one execution row"
        )
    return matches[0]


def _record_coordinate(record: PreparedRecord) -> tuple[str, int, str]:
    return (record.dataset, record.source_row, record.source_trace_id)


def _record_row_index(
    rows: Sequence[ExecutionRowEvidence],
    records: Sequence[PreparedRecord],
) -> dict[tuple[str, int, str], ExecutionRowEvidence]:
    candidates: dict[
        tuple[str, int, str], list[ExecutionRowEvidence]
    ] = defaultdict(list)
    for row in rows:
        candidates[(row.dataset, row.source_row, row.source_trace_id)].append(row)
    result: dict[tuple[str, int, str], ExecutionRowEvidence] = {}
    for record in records:
        coordinate = _record_coordinate(record)
        matches = candidates.get(coordinate, ())
        if len(matches) != 1:
            raise DeferredScopeEvidenceError(
                "A prepared record cannot be matched to one execution row"
            )
        row = matches[0]
        if (
            row.source_identity != record.source_identity
            or row.target_model != record.target_model
        ):
            raise DeferredScopeEvidenceError(
                "Prepared dependency evidence disagrees with the execution row"
            )
        result[coordinate] = row
    if len(result) != len(rows) or len(result) != len(records):
        raise DeferredScopeEvidenceError(
            "Prepared dependency accounting is incomplete"
        )
    return result


def _logical_references(value: object) -> tuple[LogicalReference, ...]:
    if isinstance(value, LogicalReference):
        return (value,)
    if isinstance(value, tuple | list):
        return tuple(
            reference
            for item in value
            for reference in _logical_references(item)
        )
    return ()


def _incoming_reference(
    owner_dataset: str,
    reference: LogicalReference,
    statuses: dict[tuple[str, bytes], set[str]],
) -> tuple[str, tuple[object, ...]] | None:
    if reference.origin == "incoming":
        if reference.dataset is None:
            raise DeferredScopeEvidenceError(
                "An incoming dependency lacks its source dataset"
            )
        return reference.dataset, tuple(reference.key)
    if reference.origin != "target_then_incoming":
        return None
    outcomes = statuses.get(
        (owner_dataset, canonical_json_bytes(portable_value(reference))),
        set(),
    )
    if len(outcomes) != 1:
        raise DeferredScopeEvidenceError(
            "Hybrid relationship resolution evidence is incomplete"
        )
    if next(iter(outcomes)) not in _INCOMING_RESOLUTION_STATUSES:
        return None
    if reference.dataset is None or reference.incoming_key is None:
        raise DeferredScopeEvidenceError(
            "A resolved incoming fallback lacks its source identity"
        )
    return reference.dataset, tuple(reference.incoming_key)


def _unique_rows(
    rows: Sequence[ExecutionRowEvidence],
) -> dict[str, ExecutionRowEvidence]:
    result: dict[str, ExecutionRowEvidence] = {}
    for row in rows:
        if not row.row_id or row.row_id in result:
            raise DeferredScopeEvidenceError("Comparison row identity is invalid")
        result[row.row_id] = row
    if not result:
        raise DeferredScopeEvidenceError("The comparison contains no prepared rows")
    return result


def _unique_issues(
    issues: Sequence[DeferredIssue],
    rows: dict[str, ExecutionRowEvidence],
) -> dict[str, DeferredIssue]:
    result: dict[str, DeferredIssue] = {}
    for issue in issues:
        if issue.issue_id in result:
            raise DeferredScopeEvidenceError("Deferred issue identity is duplicated")
        if issue.scope is DeferredIssueScope.ROW and issue.row_id not in rows:
            raise DeferredScopeEvidenceError(
                "Deferred issue evidence names an unknown comparison row"
            )
        result[issue.issue_id] = issue
    problem_rows = {
        row.row_id for row in rows.values() if row.disposition in _PROBLEM_DISPOSITIONS
    }
    evidenced_rows = {
        issue.row_id
        for issue in issues
        if issue.scope is DeferredIssueScope.ROW
    }
    if not problem_rows.issubset(evidenced_rows):
        raise DeferredScopeEvidenceError(
            "Comparison problem rows are missing root-cause evidence"
        )
    return result


def _dependency_closure(
    root_row_id: str,
    *,
    forward: dict[str, set[str]],
    reverse_group: dict[str, set[str]],
) -> frozenset[str]:
    visited = {root_row_id}
    queue = deque((root_row_id,))
    while queue:
        row_id = queue.popleft()
        for related in sorted(
            forward.get(row_id, set()) | reverse_group.get(row_id, set())
        ):
            if related in visited:
                continue
            visited.add(related)
            queue.append(related)
    return frozenset(visited)


def _row_sort_key(
    row: ExecutionRowEvidence | DeferredOmittedRow,
) -> tuple[object, ...]:
    return (row.dataset, row.source_row, row.row_id)


def _counts_by_dataset(
    rows: Sequence[ExecutionRowEvidence | DeferredOmittedRow],
) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row.dataset] += 1
    return tuple(sorted(counts.items()))


def _validate_preview_shape(preview: DeferredScopePreview) -> None:
    if (
        preview.contract_version != 1
        or not preview.comparison_id
        or _SHA256.fullmatch(preview.comparison_hash) is None
        or _SHA256.fullmatch(preview.execution_snapshot_hash) is None
        or not preview.selected_issue_ids
        or preview.selected_issue_ids
        != tuple(sorted(set(preview.selected_issue_ids)))
        or preview.prepared_record_count < 1
        or preview.already_set_aside_count < 0
        or preview.original_write_count < 0
        or preview.omitted_write_count < 0
        or preview.remaining_write_count < 0
        or preview.remaining_problem_record_count < 0
        or preview.remaining_run_issue_count < 0
        or preview.original_write_count
        != preview.omitted_write_count + preview.remaining_write_count
    ):
        raise DeferredScopeEvidenceError("Deferred-scope preview is invalid")
    omitted_by_id = {item.row_id: item for item in preview.omitted_rows}
    if (
        len(omitted_by_id) != len(preview.omitted_rows)
        or preview.counts_by_dataset != _counts_by_dataset(preview.omitted_rows)
        or preview.omitted_write_count
        != sum(
            row.disposition in _WRITE_DISPOSITIONS
            for row in preview.omitted_rows
        )
        or any(
            not row.direct_issue_ids and not row.inherited_issue_ids
            for row in preview.omitted_rows
        )
        or any(
            issue_id not in preview.selected_issue_ids
            for row in preview.omitted_rows
            for issue_id in (*row.direct_issue_ids, *row.inherited_issue_ids)
        )
    ):
        raise DeferredScopeEvidenceError(
            "Deferred-scope omitted-row accounting is invalid"
        )
    groups_by_issue = {item.issue_id: item for item in preview.groups}
    if (
        len(groups_by_issue) != len(preview.groups)
        or set(groups_by_issue) != set(preview.selected_issue_ids)
        or any(not group.row_ids for group in preview.groups)
        or any(
            row_id not in omitted_by_id
            for group in preview.groups
            for row_id in group.row_ids
        )
        or any(
            group.counts_by_dataset
            != _counts_by_dataset(
                tuple(omitted_by_id[row_id] for row_id in group.row_ids)
            )
            for group in preview.groups
        )
    ):
        raise DeferredScopeEvidenceError(
            "Deferred-scope group accounting is invalid"
        )
    preview.portable_dict()
