"""Preview Odoo-first value matches without creating new mapping decisions.

The dialog reads included incoming identities and delegates precedence,
ambiguity, and payment-term case handling to relationship health. Its protected
per-choice results are ephemeral; only reviewed Odoo aliases are saved.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from impodo.domain.mapping.contracts import (
    MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES,
    MappingDefinition,
    ResolverOrigin,
)
from impodo.domain.relationship_health import classify_relationship_health
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    _normalized_path,
    derived_dataset_links,
)
from impodo.domain.workspace.errors import WorkspaceError

from .categorical_coverage import CategoricalCoverageService


@dataclass(frozen=True)
class IncomingValueChoices:
    """Bounded incoming identities plus the lookup normalization used in staging."""

    dataset_id: str
    name: str
    key_counts: Mapping[tuple[str, ...], int]
    normalize_lookup: bool = False
    message: str = ""


def incoming_value_choices(
    coverage: CategoricalCoverageService,
    workspace_id: str,
    selection: SourceSelection,
    definition: MappingDefinition | None,
    plan: DerivedEntityPlan | None,
    *,
    dataset_id: str,
    related_model: str,
) -> IncomingValueChoices:
    """Read one selected table inside the caller's bounded local operation."""

    source = next(
        (item for item in selection.datasets if item.dataset_id == dataset_id), None
    )
    if source is None:
        raise WorkspaceError("Choose a current incoming table first")
    mapping = (
        next(
            (item for item in definition.datasets if item.dataset_id == dataset_id),
            None,
        )
        if definition
        else None
    )
    unavailable = IncomingValueChoices(
        dataset_id,
        source.name,
        {},
        message=f"Match and save {source.name} before checking its values.",
    )
    if (
        definition is None
        or definition.source_selection_hash != selection.content_hash
        or mapping is None
        or mapping.target_model != related_model
        or len(mapping.source_identity_column_keys) != 1
    ):
        return unavailable
    lookup = (
        next(
            (
                rule
                for rule, link in zip(
                    (
                        item
                        for item in plan.rules
                        if isinstance(item, (DerivedEntityRule, HierarchicalLookupRule))
                    ),
                    derived_dataset_links(plan),
                    strict=True,
                )
                if link.derived_dataset_id == dataset_id
            ),
            None,
        )
        if plan
        else None
    )
    if lookup is not None and (
        not isinstance(lookup, DerivedEntityRule) or lookup.parent_separator is not None
    ):
        return IncomingValueChoices(
            dataset_id,
            source.name,
            {},
            message="Check saved relationships to review this hierarchy's matches.",
        )
    try:
        parents = (
            coverage.included_parent_rows(workspace_id, definition.datasets)
            if mapping.row_inclusion.included_parent_field
            else {}
        )
        rows = coverage.source_identity_key_tuples(
            workspace_id,
            dataset_id,
            mapping.source_identity_column_keys,
            mapping.row_inclusion,
            included_parent=parents.get(dataset_id),
        )
        counts = Counter(
            tuple(str(value).strip() for value in row)
            for row in rows
            if row and all(value is not None and str(value).strip() for value in row)
        )
        if len(counts) > MAX_INCOMING_RELATIONSHIP_EVIDENCE_VALUES:
            raise WorkspaceError(
                "The incoming table has too many distinct identities for this check"
            )
    except WorkspaceError as error:
        return IncomingValueChoices(dataset_id, source.name, {}, message=str(error))
    return IncomingValueChoices(
        dataset_id, source.name, counts, normalize_lookup=lookup is not None
    )


def combined_value_choices(
    incoming: IncomingValueChoices,
    source_choices: Sequence[Mapping[str, object]],
    target_choices: Sequence[Mapping[str, str]],
    ambiguous_values: Sequence[str],
    *,
    owner_dataset_id: str,
    target_field: str,
    related_model: str,
    scoped_key: bool,
) -> dict[str, object]:
    """Use shared resolution rules for the exact choices shown in the dialog."""

    message = incoming.message or (
        "Check saved relationships to review matches within their scope."
        if scoped_key
        else ""
    )
    keys = {}
    for choice in source_choices:
        raw = str(choice["value"])
        path = _normalized_path(raw, None) if incoming.normalize_lookup else None
        keys[raw] = (" / ".join(path[1]),) if path else (raw,)
    targets = tuple((choice["value"],) for choice in target_choices)
    # Omitted duplicate choices must still prevent an incoming fallback.
    targets += tuple((value,) for value in ambiguous_values for _ in range(2))
    outcomes: dict[tuple[str, ...], str] = {}
    classify_relationship_health(
        owner_dataset_id=owner_dataset_id,
        target_field=target_field,
        relationship_kind="many2one",
        resolver_origin=ResolverOrigin.TARGET_THEN_DATASET,
        related_model=related_model,
        dependency_dataset_id=incoming.dataset_id,
        required=False,
        source_row_count=len(keys),
        source_key_counts=Counter(keys.values()),
        blank_row_count=0,
        incomplete_row_count=0,
        target_keys=targets,
        incoming_key_counts=incoming.key_counts if not message else {},
        choice_outcomes=outcomes,
    )
    exact = {choice["value"]: choice["value"] for choice in target_choices}
    folded = {value.casefold(): value for value in exact}
    resolutions = []
    for raw, key in keys.items():
        status = outcomes[key]
        if message and status == "missing":
            status = "unavailable"
        resolutions.append(
            {
                "source_value": raw,
                "status": status,
                "target_value": (
                    exact.get(key[0], folded.get(key[0].casefold(), ""))
                    if status == "target"
                    else ""
                ),
            }
        )
    return {
        "incoming_table": {
            "dataset_id": incoming.dataset_id,
            "name": incoming.name,
            "checked": not message,
            "message": message,
        },
        "relationship_resolutions": resolutions,
    }
