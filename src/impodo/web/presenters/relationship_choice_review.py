"""Label one authorized relationship-choice page without additional reads."""

from urllib.parse import urlencode

from impodo.domain.relationship_review import RelationshipChoiceCause

from .relationship_health import _origin_label

RELATIONSHIP_CAUSE_LABELS = {
    RelationshipChoiceCause.MISSING: "No matching record",
    RelationshipChoiceCause.AMBIGUOUS: "Matches more than one record",
    RelationshipChoiceCause.CASE_ONLY: "Case-only mismatch",
    RelationshipChoiceCause.INCOMPLETE: "Incomplete key",
    RelationshipChoiceCause.REQUIRED_BLANK: "Required value is blank",
}


def relationship_choice_page_view(
    workspace_id, selection, schema, draft, filters, page
):
    """Reuse loaded metadata for labels, correction anchors, and exact-check links."""
    datasets = {item.dataset_id: item for item in selection.datasets}
    indexes = {item.dataset_id: index for index, item in enumerate(selection.datasets)}
    mappings = {
        item.dataset_id: item for item in (draft.definition.datasets if draft else ())
    }
    models = {item.name: item for item in schema.models}

    def field_label(model_name, path):
        first, separator, remaining = path.partition(".")
        model = models.get(model_name)
        field = (
            next((item for item in model.fields if item.name == first), None)
            if model
            else None
        )
        label = field.label if field else first
        if separator:
            return (
                label
                + " → "
                + field_label(
                    field.relation if field and field.relation else "", remaining
                )
            )
        return label

    rows = []
    for choice in page.rows:
        owner = datasets.get(choice.owner_dataset_id)
        incoming = datasets.get(choice.dependency_dataset_id)
        model = models.get(choice.related_model)
        mapping = mappings.get(choice.owner_dataset_id)
        index = indexes.get(choice.owner_dataset_id, 0)
        rows.append(
            {
                "dataset": owner.name if owner else choice.owner_dataset_id,
                "field": field_label(
                    mapping.target_model if mapping else "", choice.target_field
                ),
                "related_model": model.label if model else choice.related_model,
                "origin": _origin_label(
                    choice.resolver_origin,
                    related_model_label=model.label if model else choice.related_model,
                    related_dataset_name=incoming.name
                    if incoming
                    else "incoming table",
                ),
                "key_values": tuple(
                    {"label": field_label(choice.related_model, field), "value": value}
                    for field, value in zip(choice.key_fields, choice.key, strict=True)
                ),
                "reason": RELATIONSHIP_CAUSE_LABELS[choice.cause],
                "where": {
                    "target": "Odoo",
                    "incoming": "Incoming table",
                    "both": "Odoo and incoming table",
                    "source": "Source",
                }[choice.match_origin],
                "occurrences": choice.occurrence_count,
                "affected_rows": choice.affected_row_count,
                "target_matches": choice.target_match_count,
                "incoming_matches": choice.incoming_match_count,
                "edit_url": f"/workspaces/{workspace_id}/mapping?mapping_dataset={index}#relationship-{index}-{choice.target_field}",
            }
        )
    parameters = {
        "dataset": filters.owner_dataset_id,
        "field": filters.target_field,
        "cause": filters.cause,
        "check": page.check_id,
    }

    def page_url(**cursor):
        return f"/workspaces/{workspace_id}/mapping/relationship-choices?" + urlencode(
            {
                name: value
                for name, value in {**parameters, **cursor}.items()
                if value != "" and value is not None
            }
        )

    field_options = {
        relationship.target_field: field_label(
            mapping.target_model, relationship.target_field
        )
        for mapping in mappings.values()
        for relationship in mapping.relationships
    }
    return {
        "rows": tuple(rows),
        "dataset_options": tuple(selection.datasets),
        "field_options": tuple(sorted(field_options.items())),
        "cause_options": tuple(
            (cause.value, label) for cause, label in RELATIONSHIP_CAUSE_LABELS.items()
        ),
        "previous_url": page_url(before=page.previous_before)
        if page.previous_before is not None
        else "",
        "next_url": page_url(after=page.next_after)
        if page.next_after is not None
        else "",
        "clear_url": page_url(dataset="", field="", cause=""),
    }
