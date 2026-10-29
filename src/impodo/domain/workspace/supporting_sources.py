"""Describe accepted physical and generated tables without evaluating rows.

Saved preparation rules supply stable output identities and model hints.
This catalogue intentionally excludes preview values and estimated row counts.
It is not a replacement for the effective mapping selection or preparation.
"""

from __future__ import annotations

from impodo.domain.structural import (
    ExactJoinRule,
    GroupAggregateRule,
    UnionAllRule,
    structural_dataset_id,
    structural_mapping_selection,
)
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    DerivedEntityRule,
    HierarchicalLookupRule,
    RelatedDatasetRule,
    derived_dataset_links,
    related_dataset_links,
    source_dataset_uses_for_authoring,
)
from impodo.domain.workspace.supporting_plan import (
    MAX_SUPPORTING_DATASETS,
    MAX_SUPPORTING_RULES,
    SupportingSourceDescriptor,
)


class SupportingSourceLimitError(ValueError):
    """Signal that a metadata proposal would exceed its versioned input cap."""


def supporting_source_descriptors(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> tuple[SupportingSourceDescriptor, ...]:
    """Read saved dataset shapes only; never invoke a preview or artifact reader."""
    if len(selection.datasets) > MAX_SUPPORTING_DATASETS or (
        plan is not None and len(plan.rules) > MAX_SUPPORTING_RULES
    ):
        raise SupportingSourceLimitError("Supporting source metadata exceeds its limit")
    if plan is not None and plan.source_selection_hash != selection.content_hash:
        raise ValueError("Source preparation does not match the accepted selection")
    uses = source_dataset_uses_for_authoring(selection, plan)
    rules = plan.rules if plan is not None else ()
    # Structural projection builds shapes and estimated counts from metadata.
    # It does not execute a join, load Parquet, or inspect any source row.
    shaped = structural_mapping_selection(
        selection,
        tuple(
            rule
            for rule in rules
            if isinstance(rule, (ExactJoinRule, GroupAggregateRule, UnionAllRule))
        ),
    )
    available = {item.dataset_id: item for item in shaped.datasets}
    hidden = {
        structural_dataset_id(rule)
        for rule in rules
        if isinstance(rule, ExactJoinRule) and not rule.map_output
    }
    physical_ids = {item.dataset_id for item in selection.datasets}
    result = {
        item.dataset_id: SupportingSourceDescriptor(
            item.dataset_id,
            item.name,
            "PHYSICAL" if item.dataset_id in physical_ids else "DERIVED",
            item.source_evidence_hash,
        )
        for item in shaped.datasets
        if item.dataset_id not in hidden
        and (item.dataset_id not in uses or uses[item.dataset_id].is_map_eligible)
    }
    lookup_rules = {
        rule.rule_id: rule
        for rule in rules
        if isinstance(rule, (DerivedEntityRule, HierarchicalLookupRule))
    }
    links = derived_dataset_links(plan)
    # The shared link helper preserves the effective mapping dataset identities.
    for rule, link in zip(lookup_rules.values(), links, strict=True):
        source = available.get(rule.source_dataset_id)
        if source is None:
            raise ValueError("Derived supporting table has no accepted input")
        required_columns = (
            rule.source_level_column_keys
            if isinstance(rule, HierarchicalLookupRule)
            else (rule.source_column_key,)
        )
        if not set(required_columns).issubset(
            {item.stable_key for item in source.columns}
        ):
            raise ValueError(
                "Derived supporting table uses an unavailable source field"
            )
        result[link.derived_dataset_id] = SupportingSourceDescriptor(
            link.derived_dataset_id,
            rule.output_dataset_name,
            "DERIVED",
            source.source_evidence_hash,
            rule.target_model,
        )
    split_rules = tuple(rule for rule in rules if isinstance(rule, RelatedDatasetRule))
    for rule, link in zip(split_rules, related_dataset_links(plan), strict=True):
        source = available.get(rule.source_dataset_id)
        if source is None:
            raise ValueError("Related supporting tables have no accepted input")
        for dataset_id, label in (
            (link.parent_dataset_id, rule.parent_dataset_name),
            (link.child_dataset_id, rule.child_dataset_name),
        ):
            result[dataset_id] = SupportingSourceDescriptor(
                dataset_id,
                label,
                "DERIVED",
                source.source_evidence_hash,
            )
    if len(result) > MAX_SUPPORTING_DATASETS:
        raise SupportingSourceLimitError("Supporting source metadata exceeds its limit")
    return tuple(result[key] for key in sorted(result))
