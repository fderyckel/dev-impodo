"""Project saved omission decisions before authorizing workbook originals.

These functions consume immutable comparison and preparation evidence only.
Blank prepared values alone never establish that a field was omitted by a rule.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from impodo.domain.shared.models import Classification, PreparedRecord

from .comparison_fields import (
    comparison_decision_coordinate,
    comparison_field_issue_findings,
)
from .fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
    FieldSetAsideReason,
    preparation_field_reason,
)

if TYPE_CHECKING:
    from .reports import ReviewWorkbookCellEffect

_INVALID = "Saved comparison field evidence is invalid. Compare with Odoo again."
_BLANKS = frozenset({"", "—"})
_ELIGIBLE = frozenset(
    {Classification.CREATE.value, Classification.UPDATE.value, Classification.UNCHANGED.value}
)
_NULL_POLICY_DETAIL = "The saved blank-value rule omits this field from the load."


def has_rule_omission_candidates(
    fields: FieldSetAsideEvidence,
    cell_effects: Sequence[ReviewWorkbookCellEffect] = (),
) -> bool:
    """Select the saved-manifest read only when a saved rule could omit a value."""

    return any(fields.null_omissions.values()) or any(
        not isinstance(effect.before, str)
        or not isinstance(effect.after, str)
        or effect.before.strip() not in _BLANKS and effect.after.strip() in _BLANKS
        for effect in cell_effects
    )


def comparison_omission_findings(
    decisions: Sequence[Mapping[str, Any]],
    fields: FieldSetAsideEvidence | None,
    *,
    records: Mapping[str, PreparedRecord] | None = None,
    cell_effects: Sequence[ReviewWorkbookCellEffect] = (),
) -> tuple[FieldSetAside, ...]:
    """Recover omission findings from saved issues, applied policies and effects.

    Record-bound callers prove each selected occurrence before original-value
    planning. Standalone reporting can show saved field issues without source
    authorization; applied null rules require the prepared-record index.
    """

    try:
        findings = set(comparison_field_issue_findings(decisions, fields, records=records))
        if fields is None:
            findings.update(_standalone_preparation_findings(decisions, records))
        elif records is not None:
            findings.update(_null_policy_findings(decisions, fields, records))
            findings.update(_null_effect_findings(cell_effects, fields, records, findings))
        return tuple(sorted(findings, key=_finding_key))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(_INVALID) from error


def _standalone_preparation_findings(
    decisions: Sequence[Mapping[str, Any]],
    records: Mapping[str, PreparedRecord] | None,
) -> tuple[FieldSetAside, ...]:
    findings: list[FieldSetAside] = []
    for decision in decisions:
        if decision.get("classification") != Classification.BLOCKED.value:
            continue
        for issue in _issues(decision):
            code = issue.get("code")
            reason = preparation_field_reason(code) if isinstance(code, str) else None
            source_key = issue.get("field")
            if reason is None or source_key is None or source_key == "":
                continue
            dataset, row, trace = comparison_decision_coordinate(decision, records)
            message = issue.get("message")
            if (
                not isinstance(source_key, str)
                or not source_key.strip()
                or not isinstance(message, str)
                or not message.strip()
                or issue.get("dataset") is not None and issue.get("dataset") != dataset
                or issue.get("row") is not None
                and (type(issue.get("row")) is not int or issue.get("row") != row)
            ):
                raise ValueError(_INVALID)
            findings.append(
                FieldSetAside(
                    reason, dataset, row, (source_key,), "", message,
                    source_trace_id=trace,
                )
            )
    return tuple(findings)


def _null_policy_findings(
    decisions: Sequence[Mapping[str, Any]],
    fields: FieldSetAsideEvidence,
    records: Mapping[str, PreparedRecord],
) -> tuple[FieldSetAside, ...]:
    findings: list[FieldSetAside] = []
    for decision in decisions:
        classification = decision.get("classification")
        if classification not in _ELIGIBLE:
            continue
        dataset = decision.get("dataset")
        if not isinstance(dataset, str):
            raise TypeError(_INVALID)
        targets = fields.null_omissions.get(dataset, ())
        if not targets or not _null_policy_applies(decision, fields):
            continue
        dataset, row, trace = comparison_decision_coordinate(decision, records)
        record = records[trace]
        for target in targets:
            if not _explicit_null(record, target):
                continue
            findings.append(
                FieldSetAside(
                    FieldSetAsideReason.RULE, dataset, row,
                    fields.source_keys.get((dataset, target), ()), target,
                    _NULL_POLICY_DETAIL, source_trace_id=trace,
                )
            )
    return tuple(findings)


def _null_policy_applies(
    decision: Mapping[str, Any], fields: FieldSetAsideEvidence
) -> bool:
    if decision.get("classification") != Classification.UNCHANGED.value:
        return True
    return (
        decision.get("dataset") in fields.upsert_datasets
        and type(decision.get("target_match_count")) is int
        and decision.get("target_match_count") == 1
        and not any(
            issue.get("code") == "REFERENCE_TARGET_PRECEDENCE_REUSE"
            for issue in _issues(decision)
        )
    )


def _null_effect_findings(
    cell_effects: Sequence[ReviewWorkbookCellEffect],
    fields: FieldSetAsideEvidence,
    records: Mapping[str, PreparedRecord],
    projected: set[FieldSetAside],
) -> tuple[FieldSetAside, ...]:
    known = {
        (item.source_trace_id, item.dataset, item.source_row, item.target_field)
        for item in (*fields.findings, *projected)
        if item.reason is FieldSetAsideReason.RULE
    }
    findings: list[FieldSetAside] = []
    for effect in cell_effects:
        if not isinstance(effect.before, str) or not isinstance(effect.after, str):
            raise TypeError(_INVALID)
        if effect.before.strip() in _BLANKS or effect.after.strip() not in _BLANKS:
            continue
        dataset, row, trace = comparison_decision_coordinate(
            {
                "dataset": effect.dataset,
                "source_row": effect.source_row,
                "source_trace_id": effect.source_trace_id,
            },
            records,
        )
        coordinate = (trace, dataset, row, effect.target_field)
        if coordinate in known or not _explicit_null(records[trace], effect.target_field):
            continue
        if not isinstance(effect.explanation, str):
            raise TypeError(_INVALID)
        findings.append(
            FieldSetAside(
                FieldSetAsideReason.RULE, dataset, row,
                fields.source_keys.get((dataset, effect.target_field), ()),
                effect.target_field, effect.explanation, source_trace_id=trace,
            )
        )
        known.add(coordinate)
    return tuple(findings)


def _explicit_null(record: PreparedRecord, target: str) -> bool:
    if target in record.scalar_values:
        return record.scalar_values[target] is None
    return target in record.references and record.references[target] is None


def _issues(decision: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    issues = decision.get("issues", ())
    if not isinstance(issues, (tuple, list)) or any(
        not isinstance(issue, Mapping) for issue in issues
    ):
        raise TypeError(_INVALID)
    return issues


def _finding_key(finding: FieldSetAside) -> tuple[str, int, str, tuple[str, ...], str, str, str]:
    return (
        finding.dataset,
        finding.source_row if finding.source_row is not None else -1,
        finding.target_field,
        finding.source_keys,
        finding.reason.value,
        finding.detail,
        finding.source_trace_id,
    )
