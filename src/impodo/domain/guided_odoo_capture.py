"""Propose bounded Odoo capture plans from reviewed relationship intent.

The proposer is deliberately pure: it does not save selections, contact Odoo,
or bind destination identities.  It prepares the plans that are safe to
automate, leaves relationship-scoped identity evidence protected for Stage 4,
and returns named, business-readable issues for the small remainder that still
needs a data-manager decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Iterable

from impodo.domain.mapping.create_field_policy import required_create_hook_inputs
from impodo.domain.odoo_capture import (
    MAX_ODOO_CAPTURE_DATASETS,
    MAX_ODOO_CAPTURE_FIELDS,
    OdooCaptureRole,
    OdooCaptureSelection,
)
from impodo.domain.odoo_relationship_scope import OdooRelationshipScope
from impodo.domain.odoo_source_capture import is_odoo_capture_value_field
from impodo.domain.workspace.business_keys import (
    assess_business_key_recommendation,
)
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
)


_DATASET_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")


class GuidedOdooCaptureMode(StrEnum):
    """How much source evidence one generated plan may capture."""

    FULL = "FULL"
    IDENTITY_ONLY = "IDENTITY_ONLY"
    MINIMUM_CREATE = "MINIMUM_CREATE"


class GuidedOdooCaptureIssueCode(StrEnum):
    """Stable reason why one plan still needs data-manager attention."""

    DATASET_LIMIT = "DATASET_LIMIT"
    DATASET_NAME_CONFLICT = "DATASET_NAME_CONFLICT"
    FIELD_LIMIT = "FIELD_LIMIT"
    IDENTITY_FIELD_UNAVAILABLE = "IDENTITY_FIELD_UNAVAILABLE"
    MODEL_NOT_AVAILABLE = "MODEL_NOT_AVAILABLE"
    NO_CAPTURE_FIELDS = "NO_CAPTURE_FIELDS"
    NO_SAFE_IDENTITY = "NO_SAFE_IDENTITY"
    RELATIONSHIP_SCOPE_CONFLICT = "RELATIONSHIP_SCOPE_CONFLICT"
    UNCLASSIFIED_MODEL = "UNCLASSIFIED_MODEL"


@dataclass(frozen=True, slots=True)
class GuidedOdooCapturePlanDraft:
    """One validated, persistence-neutral capture-plan recommendation."""

    model: str
    model_label: str
    dataset_name: str
    field_names: tuple[str, ...]
    mode: GuidedOdooCaptureMode
    capture_role: OdooCaptureRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", GuidedOdooCaptureMode(self.mode))
        object.__setattr__(
            self,
            "capture_role",
            OdooCaptureRole(self.capture_role),
        )
        if (
            not self.model.strip()
            or not self.model_label.strip()
            or _DATASET_NAME.fullmatch(self.dataset_name) is None
            or not self.field_names
            or self.field_names != tuple(sorted(set(self.field_names)))
            or len(self.field_names) > MAX_ODOO_CAPTURE_FIELDS
        ):
            raise ValueError("Guided Odoo capture-plan draft is invalid")
        if (
            self.mode is not GuidedOdooCaptureMode.FULL
            and self.capture_role is not OdooCaptureRole.LINKED_ONLY
        ):
            raise ValueError(
                "Identity and minimum-create capture plans must contain linked records"
            )

    @property
    def linked_only(self) -> bool:
        """Return the form-compatible linked-record choice."""

        return self.capture_role is OdooCaptureRole.LINKED_ONLY


@dataclass(frozen=True, slots=True)
class GuidedOdooCapturePlanIssue:
    """One named exception that the guided source UI can explain directly."""

    code: GuidedOdooCaptureIssueCode
    model: str | None
    model_label: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", GuidedOdooCaptureIssueCode(self.code))
        if not self.model_label.strip() or not self.reason.strip():
            raise ValueError("Guided Odoo capture-plan issue is incomplete")


@dataclass(frozen=True, slots=True)
class GuidedOdooCapturePlanProposal:
    """Safe automatic drafts, explicit exceptions, and retained manual plans."""

    drafts: tuple[GuidedOdooCapturePlanDraft, ...]
    issues: tuple[GuidedOdooCapturePlanIssue, ...]
    skipped_models: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """Return whether every missing plan can be generated automatically."""

        return not self.issues


def propose_guided_odoo_capture_plans(
    schema: OdooSchemaCatalog,
    relationship_scope: OdooRelationshipScope,
    *,
    current_selections: Iterable[OdooCaptureSelection] = (),
) -> GuidedOdooCapturePlanProposal:
    """Return safe default capture plans without persisting or guessing.

    Explicit roots and expanding related models receive full scalar evidence.
    Existing-only leaves receive only their simple scalar identity.  A
    create-if-missing leaf additionally receives required writable scalar
    values and any scalar input required by a qualified Odoo create hook.

    A business identity scoped within another record does not require manual
    Stage 2 work.  Its scalar key is selected here while the relationship scope
    remains protected capture evidence. Destination matching later binds the
    complete identity when its current capability can prove it, or names that
    identity as the remaining blocker; Stage 2 does not ask the data manager to
    configure unrelated value fields as a workaround.
    """

    models_by_name = {model.name: model for model in schema.models}
    target_models = frozenset(
        {*relationship_scope.root_models, *relationship_scope.included_models}
    )
    selections = tuple(current_selections)
    current_by_model = {selection.model: selection for selection in selections}
    skipped_models = tuple(sorted(target_models & set(current_by_model)))

    if len(target_models) > MAX_ODOO_CAPTURE_DATASETS:
        return GuidedOdooCapturePlanProposal(
            drafts=(),
            issues=(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.DATASET_LIMIT,
                    model=None,
                    model_label="Selected Odoo record types",
                    reason=(
                        f"This migration selects {len(target_models)} Odoo record "
                        f"types, but one capture can include at most "
                        f"{MAX_ODOO_CAPTURE_DATASETS}. Remove a record type or "
                        "split the migration before continuing."
                    ),
                ),
            ),
            skipped_models=skipped_models,
        )

    duplicate_current_names = _duplicates(
        selection.dataset_name for selection in selections
    )
    if duplicate_current_names:
        return GuidedOdooCapturePlanProposal(
            drafts=(),
            issues=(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.DATASET_NAME_CONFLICT,
                    model=None,
                    model_label="Saved Odoo capture plans",
                    reason=(
                        "Two saved capture plans use the same dataset name. "
                        "Give every saved plan a different name before Impodo "
                        "prepares the remaining plans."
                    ),
                ),
            ),
            skipped_models=skipped_models,
        )

    modes: dict[str, GuidedOdooCaptureMode] = {}
    mode_issues: list[GuidedOdooCapturePlanIssue] = []
    for model_name in sorted(target_models):
        if model_name in relationship_scope.root_models or (
            model_name in relationship_scope.expanding_models
        ):
            modes[model_name] = GuidedOdooCaptureMode.FULL
            continue
        reference = model_name in relationship_scope.reference_models
        create = model_name in relationship_scope.create_if_missing_models
        if reference and create:
            model = models_by_name.get(model_name)
            mode_issues.append(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.RELATIONSHIP_SCOPE_CONFLICT,
                    model=model_name,
                    model_label=model.label if model is not None else model_name,
                    reason=(
                        "This record type has conflicting existing-only and "
                        "create-if-missing choices. Review its related-data "
                        "outcome before Impodo prepares a capture plan."
                    ),
                )
            )
        elif reference:
            modes[model_name] = GuidedOdooCaptureMode.IDENTITY_ONLY
        elif create:
            modes[model_name] = GuidedOdooCaptureMode.MINIMUM_CREATE
        else:
            model = models_by_name.get(model_name)
            mode_issues.append(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.UNCLASSIFIED_MODEL,
                    model=model_name,
                    model_label=model.label if model is not None else model_name,
                    reason=(
                        "Impodo cannot tell whether this record type is a main "
                        "record or related data. Review its source role before "
                        "Impodo prepares a capture plan."
                    ),
                )
            )

    pending_models = tuple(
        model_name
        for model_name in sorted(target_models)
        if model_name not in current_by_model and model_name in modes
    )
    generated_names = {
        model_name: _default_dataset_name(model_name)
        for model_name in pending_models
    }
    reserved_names = {
        selection.dataset_name: selection.model for selection in selections
    }
    generated_name_counts: dict[str, int] = {}
    for name in generated_names.values():
        generated_name_counts[name] = generated_name_counts.get(name, 0) + 1

    drafts: list[GuidedOdooCapturePlanDraft] = []
    issues = mode_issues
    for model_name in pending_models:
        model = models_by_name.get(model_name)
        if model is None:
            issues.append(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.MODEL_NOT_AVAILABLE,
                    model=model_name,
                    model_label=model_name,
                    reason=(
                        "The current Odoo field details do not contain this "
                        "record type. Refresh the selected Odoo details before "
                        "Impodo prepares its capture plan."
                    ),
                )
            )
            continue

        dataset_name = generated_names[model_name]
        reserved_by = reserved_names.get(dataset_name)
        if (
            generated_name_counts[dataset_name] > 1
            or reserved_by is not None and reserved_by != model_name
        ):
            issues.append(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.DATASET_NAME_CONFLICT,
                    model=model_name,
                    model_label=model.label,
                    reason=(
                        f"The automatic dataset name for {model.label} is already "
                        "used by another capture plan. Give this record type a "
                        "different dataset name."
                    ),
                )
            )
            continue

        mode = modes[model_name]
        recommendation = assess_business_key_recommendation(model).preferred
        fields, field_issue = _fields_for_mode(model, mode, recommendation)
        if field_issue is not None:
            issues.append(field_issue)
            continue
        if len(fields) > MAX_ODOO_CAPTURE_FIELDS:
            issues.append(
                GuidedOdooCapturePlanIssue(
                    code=GuidedOdooCaptureIssueCode.FIELD_LIMIT,
                    model=model.name,
                    model_label=model.label,
                    reason=(
                        f"{model.label} needs {len(fields)} source fields, but one "
                        f"capture plan can include at most "
                        f"{MAX_ODOO_CAPTURE_FIELDS}. Choose the business fields "
                        "that belong in this migration."
                    ),
                )
            )
            continue
        drafts.append(
            GuidedOdooCapturePlanDraft(
                model=model.name,
                model_label=model.label,
                dataset_name=dataset_name,
                field_names=fields,
                mode=mode,
                capture_role=(
                    OdooCaptureRole.ROOT
                    if model.name in relationship_scope.root_models
                    else OdooCaptureRole.LINKED_ONLY
                ),
            )
        )

    return GuidedOdooCapturePlanProposal(
        drafts=tuple(sorted(drafts, key=lambda item: item.model)),
        issues=tuple(
            sorted(
                issues,
                key=lambda item: (
                    item.model is not None,
                    item.model or "",
                    item.code.value,
                ),
            )
        ),
        skipped_models=skipped_models,
    )


def _fields_for_mode(
    model: SchemaModel,
    mode: GuidedOdooCaptureMode,
    recommendation,
) -> tuple[tuple[str, ...], GuidedOdooCapturePlanIssue | None]:
    eligible = {
        field.name: field
        for field in model.fields
        if field.name not in {"id", "write_date"}
        and is_odoo_capture_value_field(field)
    }
    if mode is GuidedOdooCaptureMode.FULL:
        fields = tuple(sorted(eligible))
        if fields:
            return fields, None
        return (), _no_capture_fields_issue(model)

    if recommendation is None:
        return (), GuidedOdooCapturePlanIssue(
            code=GuidedOdooCaptureIssueCode.NO_SAFE_IDENTITY,
            model=model.name,
            model_label=model.label,
            reason=(
                f"Impodo found no single safe matching rule for {model.label}. "
                "Choose the field that identifies one record before Impodo "
                "prepares this capture plan."
            ),
        )

    identity_names = tuple(recommendation.key_fields)
    unavailable = tuple(name for name in identity_names if name not in eligible)
    if unavailable:
        return (), GuidedOdooCapturePlanIssue(
            code=GuidedOdooCaptureIssueCode.IDENTITY_FIELD_UNAVAILABLE,
            model=model.name,
            model_label=model.label,
            reason=(
                f"The suggested matching field for {model.label} is not "
                "available for bounded source capture. Choose another matching "
                "rule before Impodo prepares this plan."
            ),
        )

    selected = set(identity_names)
    if mode is GuidedOdooCaptureMode.MINIMUM_CREATE:
        selected.update(
            field.name
            for field in eligible.values()
            if field.required and _is_writable_scalar(field)
        )
        hook_inputs = required_create_hook_inputs(model.name, frozenset(selected))
        unavailable_hooks = tuple(
            sorted(
                name
                for name in hook_inputs
                if name not in eligible
                or not _is_writable_scalar(eligible[name])
            )
        )
        if unavailable_hooks:
            return (), GuidedOdooCapturePlanIssue(
                code=GuidedOdooCaptureIssueCode.IDENTITY_FIELD_UNAVAILABLE,
                model=model.name,
                model_label=model.label,
                reason=(
                    f"Odoo needs an additional source value to create "
                    f"{model.label}, but that value is not eligible for bounded "
                    "capture. Review this record type before continuing."
                ),
            )
        selected.update(hook_inputs)

    fields = tuple(sorted(selected))
    if not fields:
        return (), _no_capture_fields_issue(model)
    return fields, None


def _is_writable_scalar(field: SchemaField) -> bool:
    return bool(
        not field.readonly
        and field.related is not True
        and not (field.computed is True and field.has_inverse is not True)
        and field.stored is not False
    )


def _no_capture_fields_issue(model: SchemaModel) -> GuidedOdooCapturePlanIssue:
    return GuidedOdooCapturePlanIssue(
        code=GuidedOdooCaptureIssueCode.NO_CAPTURE_FIELDS,
        model=model.name,
        model_label=model.label,
        reason=(
            f"{model.label} has no scalar business fields eligible for bounded "
            "source capture. Review whether this record type belongs in the "
            "migration."
        ),
    )


def _default_dataset_name(model: str) -> str:
    return f"odoo_{model.replace('.', '_')}"[:63]


def _duplicates(values: Iterable[str]) -> frozenset[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return frozenset(duplicates)


__all__ = [
    "GuidedOdooCaptureIssueCode",
    "GuidedOdooCaptureMode",
    "GuidedOdooCapturePlanDraft",
    "GuidedOdooCapturePlanIssue",
    "GuidedOdooCapturePlanProposal",
    "propose_guided_odoo_capture_plans",
]
