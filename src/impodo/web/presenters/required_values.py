"""Label saved required-field coverage and correction links without more reads."""

from urllib.parse import urlencode

from impodo.application.workspace.mapping.required_values import (
    FieldDecisionLocation,
    RequiredValueProvider,
    RequiredValuesProjection,
    ValueCoverageState,
)

STATE_LABELS = {
    ValueCoverageState.CHOSEN: "Chosen · not checked",
    ValueCoverageState.CHECKED: "Checked",
    ValueCoverageState.MISSING: "Missing decision",
    ValueCoverageState.STALE: "Needs refresh",
    ValueCoverageState.ATTENTION: "Needs attention",
    ValueCoverageState.NOT_APPLICABLE: "Not needed",
}
PROVIDER_LABELS = {
    RequiredValueProvider.SOURCE: "Source value or rule",
    RequiredValueProvider.FIXED: "Fixed value",
    RequiredValueProvider.DEFAULT: "Checked Odoo default",
    RequiredValueProvider.MANAGED: "Odoo-managed",
    RequiredValueProvider.MISSING: "Missing",
}


def required_values_view(
    workspace_id,
    projection: RequiredValuesProjection,
    *,
    dataset="",
    state="",
    page=1,
):
    """Keep cross-table blockers visible while paging at most 100 field rows."""
    indexes = {item.dataset_id: index for index, item in enumerate(projection.tables)}
    base = f"/workspaces/{workspace_id}/mapping"
    rows, tables = [], []
    for table in projection.tables:
        index = indexes[table.dataset_id]
        edit = base + "?" + urlencode({"mapping_dataset": index})
        tables.append(
            {
                "coverage": table,
                "edit_url": edit + f"#target-identity-{index}",
                "filter_url": base
                + "/required-values?"
                + urlencode({"dataset": table.dataset_id}),
                "states": tuple(
                    (STATE_LABELS[item], table.count(item))
                    for item in (
                        ValueCoverageState.CHECKED,
                        ValueCoverageState.CHOSEN,
                        ValueCoverageState.MISSING,
                        ValueCoverageState.ATTENTION,
                        ValueCoverageState.STALE,
                    )
                    if table.count(item)
                ),
            }
        )
        for value in table.values:
            if (
                dataset
                and table.dataset_id != dataset
                or state
                and value.state.value != state
            ):
                continue
            rows.append((table, value))
    total = len(rows)
    pages = max(1, (total + 99) // 100)
    page = min(page, pages)

    def page_url(number):
        return (
            base
            + "/required-values?"
            + urlencode({"dataset": dataset, "state": state, "page": number})
        )

    return {
        "projection": projection,
        "tables": tuple(tables),
        "rows": tuple(
            _field_view(base, indexes[table.dataset_id], table, value)
            for table, value in rows[(page - 1) * 100 : page * 100]
        ),
        "matching_count": total,
        "start": (page - 1) * 100 + 1 if total else 0,
        "end": min(page * 100, total),
        "previous_url": page_url(page - 1) if page > 1 else "",
        "next_url": page_url(page + 1) if page < pages else "",
        "state_labels": STATE_LABELS,
        "state_options": tuple(
            (item.value, STATE_LABELS[item])
            for item in ValueCoverageState
            if item is not ValueCoverageState.NOT_APPLICABLE
        ),
        "dataset_filter": dataset,
        "state_filter": state,
        "review_defaults": any(
            value.review_default
            for table in projection.tables
            for value in table.values
        ),
    }


def _field_view(base, index, table, value):
    """Build correction URLs only for the visible page."""
    query: dict[str, int | str] = {
        "mapping_dataset": index,
        "relation_page" if value.relationship else "scalar_page": 1,
    }
    if not value.identity:
        query["relation_query" if value.relationship else "field_query"] = value.label
        query["relation_target" if value.relationship else "scalar_target"] = (
            value.target_field
        )
    anchor = (
        f"target-identity-{index}"
        if value.identity
        else f"relationship-{index}-{value.target_field}"
        if value.relationship
        else f"scalar-{index}-{value.target_field}"
    )
    edit_url = base + "?" + urlencode(query) + "#" + anchor
    action_label = "Review field decision"
    if value.correction is FieldDecisionLocation.REQUIREMENTS:
        edit_url, action_label = (
            base.removesuffix("/mapping") + "/schema",
            "Review Odoo requirements",
        )
    elif value.correction is FieldDecisionLocation.READONLY_RECOVERY:
        edit_url, action_label = base + "#next-step-blockers", "Review match check"
    return {
        "value": value,
        "table": table.dataset_name,
        "provider": "Unverified Odoo default"
        if value.provider is RequiredValueProvider.DEFAULT
        and not value.default_verified
        else PROVIDER_LABELS[value.provider],
        "status": STATE_LABELS[value.state],
        "edit_url": edit_url,
        "action_label": action_label,
    }
