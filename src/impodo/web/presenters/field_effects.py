"""Attach saved field outcome counts and exact review links to visible rows."""

from urllib.parse import urlencode

from impodo.domain.shared.access import Actor
from impodo.domain.staging.transformation_impact import (
    TransformationImpactCounts,
    TransformationImpactIdentity,
)


def selected_field_counts(snapshot, filters):
    """Use the same complete field counts on the exact filtered review page."""
    if (
        snapshot is None
        or snapshot.report.field_impacts is None
        or not filters.dataset
        or not filters.target_field
    ):
        return None
    return next(
        (
            item.counts
            for item in snapshot.report.field_impacts
            if item.dataset == filters.dataset
            and item.target_field == filters.target_field
        ),
        TransformationImpactCounts(),
    )


def add_field_effects(
    context,
    workspace_id: str,
    views,
    physical,
    selection,
    schema,
    governance,
    definition,
    plan,
    *,
    actor: Actor,
) -> None:
    """Query one bounded metadata page without evaluating rows or rules."""
    visible = tuple(
        (view, row)
        for view in views
        if view["active"]
        for row in view["scalar_rows"]
        if row["mapping"] and row["mapping"].value_source.value != "odoo_default"
    )
    if not visible:
        return
    identity = (
        TransformationImpactIdentity(
            physical_selection_hash=physical.content_hash,
            source_selection_hash=selection.content_hash,
            mapping_content_hash=definition.content_hash,
            schema_hash=governance.content_hash if governance else schema.content_hash,
            derived_plan_hash=plan.content_hash if plan else None,
        )
        if physical
        and selection
        and schema
        and definition
        and definition.source_selection_hash == selection.content_hash
        and definition.schema_hash
        == (governance.content_hash if governance else schema.content_hash)
        and (governance is None or governance.catalog_hash == schema.content_hash)
        else None
    )
    projection = context.field_effects.review(
        workspace_id,
        identity,
        tuple((view["source"].name, row["metadata"].name) for view, row in visible),
        actor=actor,
    )
    counts = {
        (item.dataset, item.target_field): item.counts for item in projection.fields
    }
    for view, row in visible:
        field_counts = counts.get((view["source"].name, row["metadata"].name))
        if field_counts is None and projection.available and projection.current:
            field_counts = TransformationImpactCounts()
        row["field_effects"] = {
            "status": "Current"
            if projection.current
            else "Needs refresh"
            if projection.available
            else "Not checked",
            "counts": field_counts,
            "url": f"/workspaces/{workspace_id}/mapping/transformation-impact?"
            + urlencode(
                {
                    "dataset": view["source"].name,
                    "field": row["metadata"].name,
                }
            ),
        }
