"""Author a supplied category hierarchy through the authenticated mapping route."""

from uuid import uuid4

from tests.support.browser_scenarios import POST_HEADERS


CATEGORY_CSV = (
    "RecId,Name,ParentCategory\n"
    "5637145328,Additive,5637144576\n"
    "5637144579,Bag,5637144576\n"
    "5637144580,Additive,5637144579\n"
    "5637144576,Materials,\n"
).encode()


def same_table_workspace(fixture, *, major=19, mode="LOCAL", content=CATEGORY_CSV, zero_root=False):
    """Save and check physical IDs separately from category name matching."""

    workspace_id = fixture.freeze_files({"categories": content})
    fixture.schema(workspace_id, major=major, mode=mode)
    selection = fixture.context.queries.get_source_selection(workspace_id)
    source = selection.datasets[0]
    columns = {column.source_name: column.stable_key for column in source.columns}
    entries = [
        ["csrf_token", fixture.csrf],
        ["operation_id", str(uuid4())],
        ["action", "draft"],
        ["expected_parent_version", ""],
        ["expected_working_draft_version", ""],
        ["editable_dataset_id", source.dataset_id],
        ["target_model_0", "product.category"],
        ["business_key_0", "category:name-parent"],
        ["source_identity_0", columns["RecId"]],
        ["identity_source_0_0", columns["Name"]],
        ["identity_source_0_1", columns["ParentCategory"]],
        ["identity_origin_0_1", "dataset"],
        ["identity_dataset_0_1", source.dataset_id],
    ]
    if zero_root:
        entries.append(["identity_zero_root_0_1", "1"])
    response = fixture.client.post(
        f"/workspaces/{workspace_id}/mapping/save",
        json={"entries": entries},
        headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
    )
    fixture.assertEqual(response.status_code, 200, response.text)
    return workspace_id, source
