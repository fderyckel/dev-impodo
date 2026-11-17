"""Project saved blocked relationship findings for one portable workbook.

The caller verifies the saved manifest before authorizing source-cell requests.
This module never reads artifacts, queries Odoo, or infers a failed lookup from
prepared values. Coverage binds the projection to the exact compared result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from impodo.domain.shared.models import Classification, PreparedRecord

from .fields_set_aside import FieldSetAside, FieldSetAsideEvidence, FieldSetAsideReason

COMPARISON_RELATIONSHIP_CODES = frozenset(
    {
        "REFERENCE_NOT_FOUND",
        "REFERENCE_AMBIGUOUS",
        "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED",
    }
)

_COMPARISON_REASONS = {
    "REFERENCE_NOT_FOUND": FieldSetAsideReason.UNRECOGNIZED,
    "REFERENCE_AMBIGUOUS": FieldSetAsideReason.AMBIGUOUS,
    "REFERENCE_CASE_MISMATCH_REVIEW_REQUIRED": FieldSetAsideReason.UNRECOGNIZED,
}
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_INVALID = "Saved comparison field evidence is invalid. Compare with Odoo again."


@dataclass(frozen=True, slots=True)
class ComparisonFieldCoverage:
    """Bind projected reference findings to exact immutable comparison evidence."""

    manifest_hash: str
    result_hash: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or _HASH.fullmatch(value) is None
            for value in (self.manifest_hash, self.result_hash)
        ):
            raise ValueError("Comparison field coverage hashes are invalid")


def comparison_relationship_findings(
    decisions: Sequence[Mapping[str, Any]],
    fields: FieldSetAsideEvidence | None,
    *,
    records: Mapping[str, PreparedRecord] | None = None,
) -> tuple[FieldSetAside, ...]:
    """Project only the three saved blocked-reference outcomes, including deferred rows.

    A supplied prepared-record index must prove each selected decision's exact
    trace, dataset, and physical source row. Standalone reporting can omit that
    index and still show saved findings without authorizing original values.
    Invalid selected evidence raises a value-free recovery error.
    """

    try:
        return _project_relationship_findings(decisions, fields, records)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(_INVALID) from error


def _project_relationship_findings(
    decisions: Sequence[Mapping[str, Any]],
    fields: FieldSetAsideEvidence | None,
    records: Mapping[str, PreparedRecord] | None,
) -> tuple[FieldSetAside, ...]:
    findings: set[FieldSetAside] = set()
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise TypeError(_INVALID)
        if decision.get("classification") != Classification.BLOCKED.value:
            continue
        for issue in _selected_issues(decision):
            dataset, source_row = _decision_coordinate(decision, records)
            target = issue.get("field")
            message = issue.get("message")
            if (
                not isinstance(target, str)
                or not target.strip()
                or not isinstance(message, str)
                or not message.strip()
            ):
                raise ValueError(_INVALID)
            _validate_issue_coordinate(issue, dataset, source_row)
            findings.add(
                FieldSetAside(
                    reason=_COMPARISON_REASONS[issue["code"]],
                    dataset=dataset,
                    source_row=source_row,
                    source_keys=fields.source_keys.get((dataset, target), ())
                    if fields
                    else (),
                    target_field=target,
                    detail=message,
                    comparison_issue=True,
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.dataset,
                finding.source_row,
                finding.target_field,
                finding.source_keys,
                finding.reason.value,
                finding.detail,
            ),
        )
    )


def _selected_issues(decision: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    issues = decision.get("issues", ())
    if not isinstance(issues, (tuple, list)):
        raise TypeError(_INVALID)
    selected: list[Mapping[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, Mapping):
            raise TypeError(_INVALID)
        code = issue.get("code")
        if isinstance(code, str) and code in COMPARISON_RELATIONSHIP_CODES:
            # Identity case conflicts legitimately use this code without a field.
            if issue.get("field") is None or issue.get("field") == "":
                continue
            selected.append(issue)
    return tuple(selected)


def _decision_coordinate(
    decision: Mapping[str, Any],
    records: Mapping[str, PreparedRecord] | None,
) -> tuple[str, int]:
    dataset = decision.get("dataset")
    source_row = decision.get("source_row")
    if (
        not isinstance(dataset, str)
        or not dataset.strip()
        or type(source_row) is not int
        or source_row < 1
    ):
        raise ValueError(_INVALID)
    if records is not None:
        trace = decision.get("source_trace_id")
        record = records.get(trace) if isinstance(trace, str) else None
        if (
            record is None
            or not trace
            or record.source_trace_id != trace
            or record.dataset != dataset
            or record.source_row != source_row
        ):
            raise ValueError(_INVALID)
    return dataset, source_row


def _validate_issue_coordinate(
    issue: Mapping[str, Any],
    dataset: str,
    source_row: int,
) -> None:
    issue_dataset = issue.get("dataset")
    issue_row = issue.get("row")
    if (
        issue_dataset is not None
        and issue_dataset != dataset
        or issue_row is not None
        and (type(issue_row) is not int or issue_row != source_row)
    ):
        raise ValueError(_INVALID)
