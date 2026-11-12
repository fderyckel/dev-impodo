"""Portable rules for existing parents in generated hierarchies.

See docs/developer/workflow/01-source-data.md. Numeric Odoo identifiers never
belong in these rules; comparison resolves their complete business paths anew.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from impodo.domain.shared.models import (
    BusinessReference,
    Classification,
    Decision,
    Issue,
    PreparedRecord,
)

_FIELD = re.compile(r"[a-z_][a-z0-9_]{0,127}")
MAX_PARENT_DEPTH = 20


@dataclass(frozen=True, slots=True)
class HierarchyParentPolicy:
    """Describe a checked parent relationship and the levels used as references."""

    parent_field: str
    fixed_path: tuple[str, ...] = ()
    existing_source_levels: int = 0
    company_field: str | None = None
    company_name: str | None = None
    fixed_companies: tuple[str | None, ...] = ()
    contract_version: int = 1
    name_field: str = "name"

    def __post_init__(self) -> None:
        if (
            self.contract_version != 1
            or _FIELD.fullmatch(self.parent_field) is None
            or _FIELD.fullmatch(self.name_field) is None
        ):
            raise ValueError("The hierarchy parent relationship is invalid")
        if (
            type(self.existing_source_levels) is not int
            or not 0 <= self.existing_source_levels <= 5
        ):
            raise ValueError("Choose between zero and five existing source levels")
        if len(self.fixed_path) > MAX_PARENT_DEPTH or any(
            not value.strip() or len(value) > 500 for value in self.fixed_path
        ):
            raise ValueError("The existing Odoo parent path is invalid")
        if self.company_field is not None and (
            _FIELD.fullmatch(self.company_field) is None
            or not self.company_name
            or len(self.company_name) > 200
            or len(self.fixed_companies) != len(self.fixed_path)
        ):
            raise ValueError("Choose the company for this hierarchy")
        if self.company_field is None and (
            self.company_name is not None or self.fixed_companies
        ):
            raise ValueError("This hierarchy has no company relationship")
        if any(
            value is not None
            and (not isinstance(value, str) or not value.strip() or len(value) > 200)
            for value in self.fixed_companies
        ):
            raise ValueError("The existing-parent company names are invalid")
        if not self.fixed_path and not self.existing_source_levels:
            raise ValueError("Choose an existing parent or an existing source level")

    @property
    def reference_depth(self) -> int:
        """Return the prefix that must exist and must never be written."""
        return len(self.fixed_path) + self.existing_source_levels

    def company_for_depth(self, depth: int) -> str | None:
        """Keep preview and staging company assignment identical."""
        if depth < 1:
            raise ValueError("Hierarchy depth must be positive")
        if self.company_field is None:
            return None
        return (
            self.fixed_companies[depth - 1]
            if depth <= len(self.fixed_path)
            else self.company_name
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize reusable decisions without target-specific record IDs."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> HierarchyParentPolicy:
        """Restore the closed versioned parent policy."""
        if not isinstance(value, Mapping) or set(value) != {
            "parent_field",
            "fixed_path",
            "existing_source_levels",
            "company_field",
            "company_name",
            "fixed_companies",
            "contract_version",
            "name_field",
        }:
            raise ValueError("The existing-parent policy is invalid")
        path, companies = value["fixed_path"], value["fixed_companies"]
        if not isinstance(path, (list, tuple)) or not all(
            isinstance(item, str) for item in path
        ):
            raise ValueError("The existing-parent path is invalid")
        if not isinstance(companies, (list, tuple)) or not all(
            item is None or isinstance(item, str) for item in companies
        ):
            raise ValueError("The existing-parent companies are invalid")
        if (
            type(value["existing_source_levels"]) is not int
            or type(value["contract_version"]) is not int
        ):
            raise ValueError("The existing-parent policy version is invalid")
        return cls(
            parent_field=str(value["parent_field"]),
            fixed_path=tuple(path),
            existing_source_levels=value["existing_source_levels"],
            company_field=str(value["company_field"])
            if value["company_field"] is not None
            else None,
            company_name=str(value["company_name"])
            if value["company_name"] is not None
            else None,
            fixed_companies=tuple(companies),
            contract_version=value["contract_version"],
            name_field=str(value["name_field"]),
        )

    def is_reference(
        self, record: PreparedRecord, scope_fields: tuple[str, ...]
    ) -> bool:
        """Recognize a resolved prefix by its governed parent scope."""
        if self.parent_field not in scope_fields or len(scope_fields) != len(
            record.target_scope
        ):
            raise ValueError(
                "The hierarchy identity must include its parent relationship"
            )
        parent = record.target_scope[scope_fields.index(self.parent_field)]
        depth = 1
        while parent is not None:
            if (
                not isinstance(parent, BusinessReference)
                or self.parent_field not in parent.target_scope_fields
                or len(parent.scope) != len(parent.target_scope_fields)
            ):
                raise ValueError("The hierarchy parent path is unresolved")
            depth += 1
            if depth > MAX_PARENT_DEPTH + 5:
                raise ValueError("The hierarchy parent path is too deep")
            parent = parent.scope[parent.target_scope_fields.index(self.parent_field)]
        return depth <= self.reference_depth


def block_hierarchy_descendants(
    records: tuple[PreparedRecord, ...],
    decisions: list[Decision],
    hierarchy_datasets: frozenset[str],
) -> list[Decision]:
    """Hold descendants whose required existing ancestor failed comparison."""
    if not hierarchy_datasets:
        return decisions

    def identity_value(value: object) -> object:
        if isinstance(value, BusinessReference):
            return (value.model, identity_value(value.key), identity_value(value.scope))
        if isinstance(value, tuple):
            return tuple(identity_value(part) for part in value)
        return value

    by_row = {(record.dataset, record.source_row): record for record in records}
    blocked = {
        (
            decision.dataset,
            by_row[(decision.dataset, decision.source_row)].target_model,
            identity_value(decision.business_identity),
            identity_value(decision.business_scope),
        )
        for decision in decisions
        if decision.dataset in hierarchy_datasets
        and decision.classification
        in {Classification.BLOCKED, Classification.AMBIGUOUS}
    }

    def has_blocked_parent(value: object, dataset: str) -> bool:
        if not isinstance(value, BusinessReference):
            return False
        return (
            dataset,
            value.model,
            identity_value(value.key),
            identity_value(value.scope),
        ) in blocked or any(
            has_blocked_parent(part, dataset) for part in (*value.key, *value.scope)
        )

    return [
        replace_decision_with_parent_issue(decision)
        if decision.dataset in hierarchy_datasets
        and decision.classification
        in {Classification.CREATE, Classification.UPDATE, Classification.UNCHANGED}
        and any(
            has_blocked_parent(value, decision.dataset)
            for value in (*decision.business_identity, *decision.business_scope)
        )
        else decision
        for decision in decisions
    ]


def replace_decision_with_parent_issue(decision: Decision) -> Decision:
    """Keep source evidence while preventing a child from being written early."""
    return replace(
        decision,
        classification=Classification.BLOCKED,
        differences=(),
        issues=(
            *decision.issues,
            Issue(
                code="HIERARCHY_EXISTING_ANCESTOR_NEEDS_ATTENTION",
                message="An existing parent in this path needs attention. Correct its path, company, or Odoo access, then check again.",
                dataset=decision.dataset,
                row=decision.source_row,
            ),
        ),
    )
