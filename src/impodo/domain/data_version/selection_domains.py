"""Validate Odoo selection fields from captured and live metadata.

Selection choices are target metadata, not transformation instructions.  This
module therefore reads the fields named by the shared compiled plan, indexes
their live choice codes once, and validates every final prepared value without
performing Odoo calls or adding writer-specific semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.shared.models import Issue, PreparedRecord, Severity
from impodo.domain.recipe.profile import DatasetSpec
from impodo.domain.workspace.contracts import OdooSchemaCatalog


@dataclass(frozen=True, slots=True)
class _ValueBinding:
    """Locate one target field's final value in a prepared record."""

    field: str
    source: Literal["scalar", "identity", "scope"]
    position: int | None = None

    def value(self, record: PreparedRecord) -> Any:
        if self.source == "scalar":
            return record.scalar_values.get(self.field)
        values = (
            record.target_identity
            if self.source == "identity"
            else record.target_scope
        )
        return values[int(self.position)]


@dataclass(frozen=True, slots=True)
class _LiveSelection:
    """One indexed live selection domain used for row validation."""

    binding: _ValueBinding
    codes: frozenset[str]


def apply_live_selection_domains(
    plan: CompiledMigrationPlan,
    records: Iterable[PreparedRecord],
    snapshot: MetadataSnapshot,
) -> tuple[PreparedRecord, ...]:
    """Attach row-local errors for final values outside live Odoo choices.

    ``None`` means no proposed value and is intentionally not a choice-domain
    error. Every other final value must match an Odoo choice code exactly.
    Models, fields, and choice sets are indexed before rows are visited, so
    validation is linear and never performs per-row target lookups.
    """

    domains = _live_domains(plan, snapshot)
    result: list[PreparedRecord] = []
    for record in records:
        issues = list(record.issues)
        for domain in domains.get(record.dataset, ()):
            value = domain.binding.value(record)
            if value is None or str(value) in domain.codes:
                continue
            issues.append(
                Issue(
                    code="TARGET_SELECTION_VALUE_UNAVAILABLE",
                    message=(
                        f"{value!r} is not available for "
                        f"{record.target_model}.{domain.binding.field} in "
                        "the current Odoo choices"
                    ),
                    dataset=record.dataset,
                    row=record.source_row,
                    field=domain.binding.field,
                )
            )
        result.append(
            replace(record, issues=tuple(issues))
            if len(issues) != len(record.issues)
            else record
        )
    return tuple(result)


def validate_selection_metadata_drift(
    plan: CompiledMigrationPlan,
    live: MetadataSnapshot,
    captured: OdooSchemaCatalog | None,
) -> tuple[Issue, ...]:
    """Compare consequential selection metadata captured then and live now.

    Choice labels are deliberately ignored: codes are authoritative and labels
    are display-only. A transition to or from ``selection`` blocks because it
    removes the domain contract. A changed code set is reported as a warning;
    any prepared value using a removed code is separately blocked by
    :func:`apply_live_selection_domains`.
    """

    if captured is None:
        return ()
    captured_models = {
        model.name: {field.name: field for field in model.fields}
        for model in captured.models
    }
    issues: list[Issue] = []
    for dataset in plan.datasets:
        live_model = live.models.get(dataset.target.model)
        captured_fields = captured_models.get(dataset.target.model, {})
        if live_model is None:
            continue
        for binding in _value_bindings(dataset):
            captured_field = captured_fields.get(binding.field)
            live_field = live_model.fields.get(binding.field)
            if captured_field is None or live_field is None:
                continue
            captured_is_selection = captured_field.type == "selection"
            live_is_selection = live_field.type == "selection"
            if captured_is_selection != live_is_selection:
                issues.append(
                    Issue(
                        code="TARGET_SELECTION_FIELD_DRIFT",
                        message=(
                            f"{dataset.target.model}.{binding.field} changed "
                            f"from {captured_field.type} to {live_field.type}; "
                            "recapture the Odoo fields and review the mapping"
                        ),
                        dataset=dataset.name,
                        field=binding.field,
                    )
                )
                continue
            if not captured_is_selection:
                continue
            captured_codes = _choice_codes(captured_field.selection)
            live_codes = _choice_codes(live_field.selection)
            if captured_codes != live_codes:
                removed = len(captured_codes - live_codes)
                added = len(live_codes - captured_codes)
                issues.append(
                    Issue(
                        code="TARGET_SELECTION_CHOICES_CHANGED",
                        message=(
                            f"Current Odoo choices for {dataset.target.model}."
                            f"{binding.field} changed since mapping "
                            f"({removed} removed, {added} added)"
                        ),
                        severity=Severity.WARNING,
                        dataset=dataset.name,
                        field=binding.field,
                    )
                )
    return tuple(issues)


def live_selection_metadata_issues(
    plan: CompiledMigrationPlan,
    snapshot: MetadataSnapshot,
) -> tuple[Issue, ...]:
    """Reject ambiguous live selection metadata before row validation."""

    issues: list[Issue] = []
    for dataset in plan.datasets:
        model = snapshot.models.get(dataset.target.model)
        if model is None:
            continue
        for binding in _value_bindings(dataset):
            field = model.fields.get(binding.field)
            if field is None or field.type != "selection":
                continue
            codes = tuple(str(code) for code, _label in field.selection)
            if len(set(codes)) != len(codes):
                issues.append(
                    Issue(
                        code="TARGET_SELECTION_METADATA_INVALID",
                        message=(
                            f"{dataset.target.model}.{binding.field} returned "
                            "duplicate Odoo choice codes"
                        ),
                        dataset=dataset.name,
                        field=binding.field,
                    )
                )
    return tuple(issues)


def _live_domains(
    plan: CompiledMigrationPlan,
    snapshot: MetadataSnapshot,
) -> Mapping[str, tuple[_LiveSelection, ...]]:
    domains: dict[str, tuple[_LiveSelection, ...]] = {}
    for dataset in plan.datasets:
        model = snapshot.models.get(dataset.target.model)
        if model is None:
            continue
        selected = []
        for binding in _value_bindings(dataset):
            field = model.fields.get(binding.field)
            if field is None or field.type != "selection":
                continue
            selected.append(
                _LiveSelection(
                    binding=binding,
                    codes=_choice_codes(field.selection),
                )
            )
        domains[dataset.name] = tuple(selected)
    return domains


def _value_bindings(dataset: DatasetSpec) -> tuple[_ValueBinding, ...]:
    bindings = {
        field: _ValueBinding(field=field, source="scalar")
        for field in dataset.fields
    }
    for source, components in (
        ("identity", dataset.target_identity.components),
        ("scope", dataset.target_identity.scope),
    ):
        position = 0
        for component in components:
            if component.resolve is None:
                for field in component.target_fields:
                    bindings.setdefault(
                        field,
                        _ValueBinding(
                            field=field,
                            source=source,
                            position=position,
                        ),
                    )
                    position += 1
            else:
                position += 1
    return tuple(bindings[field] for field in sorted(bindings))


def _choice_codes(choices: Iterable[tuple[str, str]]) -> frozenset[str]:
    return frozenset(str(code) for code, _label in choices)
