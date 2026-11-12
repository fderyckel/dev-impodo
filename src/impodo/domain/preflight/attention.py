"""Describe a complete correction worklist without granting load approval.

The application binds these display-only records to current saved evidence.
Artifact adapters add permitted original file values and Excel presentation.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from impodo.domain.source_snapshot import SourceSnapshot

from ..shared.models import Classification
from .reports import plain_readiness_guidance


@dataclass(frozen=True, slots=True)
class AttentionRecord:
    """One affected prepared record with complete contributing source locations."""

    row_id: str
    dataset: str
    record: str
    source_row: int
    locations: Mapping[str, tuple[int, ...]]
    values: Mapping[str, str]
    labels: Mapping[str, str]
    field_sources: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class AttentionFinding:
    """One saved problem and its practical correction advice."""

    row_id: str
    dataset: str
    fields: tuple[str, ...]
    priority: str
    problem: str
    explanation: str
    action: str
    correction: str
    owner: str
    effect: str
    code: str
    related_rows: tuple[str, ...] = ()
    group_id: str = ""


@dataclass(frozen=True, slots=True)
class AttentionProblem:
    """Share a correction's next action between the page and its workbook."""

    problem: str
    action: str
    correction: str
    affected_records: int
    findings: int


@dataclass(frozen=True, slots=True)
class AttentionExport:
    """Exact, portable display evidence for one read-only Excel download."""

    workspace_id: str
    project: str
    data_version_id: str
    company: str
    prepared_at: datetime
    comparison_at: datetime | None
    quality_run_id: str
    comparison_id: str
    records: tuple[AttentionRecord, ...]
    findings: tuple[AttentionFinding, ...]
    snapshots: tuple[SourceSnapshot, ...]
    source_names: Mapping[str, str]
    source_columns: Mapping[str, str]
    filtered_count: int
    restricted: bool = False
    input_bindings: tuple[str, ...] = ()
    business_snapshot_hash: str = ""
    business_assessment_hash: str = ""
    derived_plan_hash: str = ""
    source_origins: Mapping[str, Mapping[str, tuple[str, ...]]] = field(
        default_factory=dict
    )

    @property
    def affected_record_count(self) -> int:
        """Count affected records once even when several findings describe them."""

        return len({finding.row_id for finding in self.findings if finding.row_id})

    @property
    def problem_summaries(self) -> tuple[AttentionProblem, ...]:
        """Group the same saved correction advice without exposing row values."""

        groups: dict[tuple[str, str, str], list[AttentionFinding]] = defaultdict(list)
        for finding in self.findings:
            groups[(finding.problem, finding.action, finding.correction)].append(
                finding
            )
        return tuple(
            AttentionProblem(
                problem,
                action,
                correction,
                len({item.row_id for item in members if item.row_id}),
                len(members),
            )
            for (problem, action, correction), members in sorted(groups.items())
        )


def attention_guidance(code: str) -> tuple[str, str, str]:
    """Name the problem, a conditional next action, and the correction location."""

    if code == "TARGET_FIELD_UNKNOWN":
        code = "TARGET_FIELD_MISSING"
    guidance = {
        "TARGET_SNAPSHOT_INCOMPLETE": (
            "Could not verify the Odoo setup",
            "Check Odoo access and refresh Odoo data, then compare again. Incomplete evidence does not establish that a record is missing.",
            "Odoo data → Check connection and selected record types",
        ),
        "TARGET_REFERENCE_UNRESOLVED": (
            "Could not verify the related record",
            "Check access to the related Odoo data and its complete matching rule, then compare again. Do not create a replacement based on this result.",
            "Odoo data and Match data → Linked records",
        ),
        "TARGET_MODEL_UNKNOWN": (
            "Selected Odoo record type is unavailable",
            "Refresh Odoo data and check access to the selected record type with the Odoo owner before comparing again.",
            "Odoo data",
        ),
        "SOURCE_IDENTITY_INVALID": (
            "Row identifier is empty or invalid",
            "Complete the identifying values, or review the selected identifier columns. Then prepare the data again.",
            "Source data or Match data → Unique row identifier",
        ),
        "SOURCE_IDENTITY_DUPLICATE": (
            "Source rows use the same identifier",
            "Review the related rows together. Correct the identifier if they are different records, or review which row to keep if one repeats another.",
            "Source data or Match data → Unique row identifier",
        ),
        "POST_TRANSFORM_IDENTITY_COLLISION": (
            "Prepared records use the same Odoo match",
            "Review the related rows, all matching columns, and their rules. Correct the source or matching rule if it combines different records.",
            "Source data or Match data → Odoo matching rule",
        ),
        "REFERENCE_NOT_FOUND": (
            "No related record matched the checked Odoo data",
            "Check the source value and company. Choose the intended existing record, or ask the responsible owner whether the missing record should be created. Then compare again.",
            "Match data → Match values, or Odoo",
        ),
        "INCOMING_RELATIONSHIP_MISSING": (
            "Related source record is missing",
            "Check the linked source table and matching columns. Supply or correct the related record, then prepare the data again.",
            "Source data or Match data → Linked records",
        ),
        "REFERENCE_AMBIGUOUS": (
            "Several related records match",
            "Review the matching records and choose the intended one, or review the columns used to distinguish them.",
            "Match data → Match values",
        ),
        "INCOMING_RELATIONSHIP_AMBIGUOUS": (
            "Several related source records match",
            "Review the related source rows and use the complete identifier for the intended record.",
            "Source data or Match data → Linked records",
        ),
        "TARGET_IDENTITY_AMBIGUOUS": (
            "Several Odoo records match this record",
            "Review the matching Odoo records and the complete matching rule, including company or parent where applicable.",
            "Match data → Odoo matching rule, or Odoo",
        ),
        "REFERENCE_COMPANY_MISMATCH": (
            "Related record is incompatible with this company",
            "Check the selected company and choose a related record that Odoo allows for that company.",
            "Match data → Company for this load and Match values",
        ),
        "SOURCE_REQUIRED_VALUE_MISSING": (
            "Required value is missing",
            "Complete the source value or select the column containing it, then prepare the data again.",
            "Source data or Match data",
        ),
        "REQUIRED_ON_CREATE_MISSING": (
            "Odoo needs a value to create this record",
            "Supply the required value or select the source column containing it, then prepare and compare again.",
            "Source data or Match data",
        ),
        "SOURCE_TYPE_INVALID": (
            "Value has an invalid format",
            "Correct the source value or review its conversion rule, then prepare the data again.",
            "Source data or Match data",
        ),
        "SOURCE_FIELD_MISSING": (
            "Selected source column is unavailable",
            "Select the current source column and prepare the data again.",
            "Match data",
        ),
        "TARGET_FIELD_MISSING": (
            "Selected Odoo field is unavailable",
            "Refresh Odoo data and choose an available field in Match data. Ask the Odoo owner if the selected field should exist.",
            "Odoo data, then Match data",
        ),
        "CONTROL_TOTAL_MISMATCH": (
            "Prepared total differs from the expected total",
            "Review the affected column, included source rows, and declared total. Correct the data or the intended total and prepare again.",
            "Source data or Match data → Control totals",
        ),
        "TARGET_SELECTION_FIELD_DRIFT": (
            "Odoo field changed",
            "Refresh Odoo data and review the affected field match before comparing again.",
            "Odoo data, then Match data",
        ),
        "TARGET_NUMERIC_PRECISION_LOSS": (
            "Odoo cannot retain this number exactly",
            "Review the prepared number and Odoo field capacity. Agree the intended precision before changing the data or rule.",
            "Source data, Match data, or Odoo field setup",
        ),
        "BUSINESS_CHECK_FAILED": (
            "Value does not meet a saved data check",
            "Review the named check and affected values with the data owner. Correct the data or the intended check, then prepare again.",
            "Source data or Prepare data → Data checks",
        ),
    }
    if code in guidance:
        return guidance[code]
    if code in {
        "REFERENCE_BLOCKED_BY_DEPENDENCY",
        "INCOMING_RELATIONSHIP_PARENT_SET_ASIDE",
        "INCOMING_IDENTITY_DEPENDENT_SET_ASIDE",
    }:
        return (
            "Linked record needs correction first",
            "Correct the related record shown in this report first, then prepare and compare again.",
            "Related source record or its field match",
        )
    reason, action = plain_readiness_guidance(code, Classification.BLOCKED)
    if reason != "This row cannot be processed safely.":
        return reason, action, "Source data or the affected choice in Match data"
    return (
        "Saved check needs attention",
        "Review the affected columns and saved finding in Impodo. Ask the data owner to confirm the correction, then repeat the indicated check.",
        "Final review → Affected record and its correction action",
    )
