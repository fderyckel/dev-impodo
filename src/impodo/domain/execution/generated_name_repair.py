"""Plan display-name corrections from verified creates without writing to Odoo.

Only exact saved receipts and the original generated-list display values can
produce a correction. The plan retains the IDs of existing records.
See docs/developer/contracts/execution-and-reconciliation.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.workspace.derived_entities import (
    DerivedDatasetLink,
    DerivedEntityCandidate,
    DerivedEntityRule,
)

from .models import ExecutionRowAttempt, ExecutionRowStatus


@dataclass(frozen=True, slots=True)
class GeneratedNameRepair:
    """One proposed spelling change tied to an already verified Odoo receipt."""

    row_id: str
    odoo_id: int
    external_id: str
    verified_name: str
    original_name: str


def plan_generated_name_repairs(
    *,
    mapping: DatasetMapping,
    link: DerivedDatasetLink,
    rule: DerivedEntityRule,
    candidates: Sequence[DerivedEntityCandidate],
    attempts: Sequence[ExecutionRowAttempt],
    verified_row_ids: frozenset[str],
) -> tuple[GeneratedNameRepair, ...]:
    """Reject stale, partial, ambiguous, or unverified evidence before export."""

    if (
        link.hierarchical
        or rule.parent_separator is not None
        or rule.target_model != link.target_model
        or rule.target_name_field != link.target_name_field
        or rule.source_column_key != link.source_column_key
        or mapping.dataset_id != link.derived_dataset_id
        or mapping.target_model != link.target_model
        or mapping.source_identity_column_keys != (link.canonical_key_column_key,)
        or len(mapping.target_identity) != 1
        or mapping.target_scope
        or mapping.target_identity[0].source_column_keys
        != (link.canonical_key_column_key,)
        or mapping.target_identity[0].target_fields != (link.target_name_field,)
        or mapping.target_identity[0].resolver is not None
        or mapping.fields
    ):
        raise ValueError(
            "This name repair requires the exact generated-key-to-name mapping"
        )
    ordered = tuple(sorted(candidates, key=lambda candidate: candidate.canonical_key))
    if len({candidate.canonical_key for candidate in ordered}) != len(ordered) or any(
        candidate.requires_alias_review for candidate in ordered
    ):
        raise ValueError("Generated name repair keys are ambiguous")
    rows = tuple(row for row in attempts if row.dataset == rule.output_dataset_name)
    if len(rows) != len(ordered) or len({row.source_row for row in rows}) != len(rows):
        raise ValueError("Generated name repair does not cover every original record")
    repairs: list[GeneratedNameRepair] = []
    ids: set[int] = set()
    external_ids: set[str] = set()
    for row in sorted(rows, key=lambda attempt: attempt.source_row):
        if (
            row.status is not ExecutionRowStatus.COMMITTED
            or row.operation != "CREATE"
            or row.row_id not in verified_row_ids
            or row.target_model != mapping.target_model
            or row.field_names != (link.target_name_field,)
            or row.odoo_id is None
            or row.odoo_id <= 0
            or row.odoo_id in ids
            or not row.proposed_external_id
            or row.proposed_external_id in external_ids
            or not 2 <= row.source_row <= len(ordered) + 1
        ):
            raise ValueError(
                "Generated name repair requires unique verified create receipts"
            )
        ids.add(row.odoo_id)
        external_ids.add(row.proposed_external_id)
        candidate = ordered[row.source_row - 2]
        if candidate.canonical_key != candidate.name:
            repairs.append(
                GeneratedNameRepair(
                    row.row_id,
                    row.odoo_id,
                    row.proposed_external_id,
                    candidate.canonical_key,
                    candidate.name,
                )
            )
    return tuple(repairs)
