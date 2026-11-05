"""Publish a real identity check over accepted fictional source rows."""

from datetime import UTC, datetime

from impodo.domain.mapping.contracts import (
    DatasetMapping, IdentityComponentMapping, RelationshipMapping, RelationshipResolver,
    ResolverOrigin, RowInclusionPolicy,
)
from impodo.domain.matching_order import (
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
)
from impodo.domain.odoo.contracts import RecordSnapshot
from impodo.domain.schema.governance import BusinessKeyDefinition, BusinessKeyStatus
from impodo.domain.shared.models import TargetRecord
from impodo.domain.workspace.contracts import SchemaField, SchemaModel
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.recipe_lifecycle import metadata_for_schema


def duplicate_identity_workspace(fixture, *, repeated_count=105, unique_source_ids=False,
    major=19, mode="LOCAL", children=False, policy_factory=None,
    source_identity_names=None, child_parent_values=None, source_id_values=None):
    rows = [("DUP", "BE")] * repeated_count + [
        ("DUP", "LU"),
        ("TARGET", "BE"),
        ("UNIQUE", "BE"),
        ("", "BE"),
    ]
    data = "Reference,Site\n" + "\n".join(",".join(row) for row in rows) + "\n"
    if unique_source_ids:
        data = "SourceId,Reference,Site,Description\n" + "\n".join(
            f"{source_id_values[index - 1] if source_id_values else f'ROW-{index}'},{reference},{site},Version {index}" for index, (reference, site) in enumerate(rows, 1)
        ) + "\n"
    files = {"products": data.encode()}
    if children:
        parents = child_parent_values or ("ROW-1", "ROW-1", "ROW-2")
        files["lines"] = ("Line,Parent\n" + "\n".join(f"L-{index},{parent}" for index, parent in enumerate(parents, 1)) + "\n").encode()
    workspace_id = fixture.freeze_files(files)
    model = SchemaModel(
        "x.product",
        "Product",
        (
            SchemaField("code", "Reference", "char", True, False, None, None, ()),
            SchemaField("site", "Site", "char", False, False, None, None, ()),
        ),
    )
    schema = fixture.schema(
        workspace_id,
        major=major,
        mode=mode,
        extra_models=(model,),
        extra_keys=(
            BusinessKeyDefinition(
                "product:reference-site",
                "x.product",
                ("code",),
                scope_fields=("site",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
    )
    context, actor = fixture.context, fixture.actor
    selection = context.queries.get_mapping_source_selection(workspace_id)
    dataset = next(item for item in selection.datasets if item.name == "products")
    columns = {column.source_name: column.stable_key for column in dataset.columns}
    reference, site = columns["Reference"], columns["Site"]
    mappings = [DatasetMapping(
        dataset.dataset_id, "x.product",
        source_identity_column_keys=tuple(columns[name] for name in source_identity_names)
            if source_identity_names is not None else (columns["SourceId"],) if unique_source_ids else (reference, site),
        target_identity=(IdentityComponentMapping((reference,), ("code",)),),
        target_scope=(IdentityComponentMapping((site,), ("site",)),),
        row_inclusion=policy_factory(columns) if policy_factory else RowInclusionPolicy(),
    )]
    if children:
        child = next(item for item in selection.datasets if item.name == "lines")
        line, parent = (column.stable_key for column in child.columns)
        mappings.append(DatasetMapping(child.dataset_id, "x.product",
            source_identity_column_keys=(line,),
            relationships=(RelationshipMapping(target_field="parent_id", source_column_keys=(parent,), kind="many2one",
                resolver=RelationshipResolver(origin=ResolverOrigin.DATASET, dataset_id=dataset.dataset_id)),)))
    draft = context.mapping_workspace.save_working_draft(
        workspace_id,
        datasets=tuple(mappings),
        expected_version=None,
        actor=actor,
    )
    governance = context.queries.get_schema_governance(workspace_id)
    service = context.matching_order
    local = service.recommend(selection, schema, draft.definition)
    prepared = run_local_operation_sync(
        service.prepare_live_check,
        workspace_id,
        selection,
        schema,
        governance,
        draft,
        local,
        actor=actor,
    )
    now = datetime.now(UTC)
    repository = service._preferences
    repository.begin_check(
        workspace_id,
        MatchingOrderCheckAttempt(
            prepared.check_id,
            workspace_id,
            MatchingOrderCheckStatus.QUEUED,
            MatchingOrderCheckPhase.QUEUED,
            "Waiting to check Odoo",
            0,
            now,
            now,
        ),
        actor=actor,
    )
    reads = []

    def reader(requirements):
        reads.append(requirements)
        metadata = metadata_for_schema(
            schema,
            {
                request.model: request.fields
                for request in requirements.metadata_requests
            },
        )
        records = tuple(
            TargetRecord("x.product", 92100001 + index, {"code": code, "site": scope})
            for index, (code, scope) in enumerate(
                (
                    ("DUP", "BE"),
                    ("DUP", "BE"),
                    ("DUP", "LU"),
                    ("TARGET", "BE"),
                    ("TARGET", "BE"),
                )
            )
        )
        return metadata, RecordSnapshot(
            metadata.fingerprint,
            {"x.product": records},
            {"x.product": ("code", "site")},
        )

    fixture.identity_group_reader = reader
    service._run_live_check(prepared, reader)
    check = repository.get_current_check(workspace_id)
    fixture.assertIsNotNone(
        check, repository.get_check_attempt(workspace_id, prepared.check_id)
    )
    fixture.assertFalse(check.schema_changed)
    return workspace_id, dataset, check, reads
