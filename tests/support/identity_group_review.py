"""Publish a real identity check over accepted fictional source rows."""

from datetime import UTC, datetime

from impodo.domain.mapping.contracts import DatasetMapping, IdentityComponentMapping
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


def duplicate_identity_workspace(fixture, *, repeated_count=105):
    rows = [("DUP", "BE")] * repeated_count + [
        ("DUP", "LU"),
        ("TARGET", "BE"),
        ("UNIQUE", "BE"),
        ("", "BE"),
    ]
    data = "Reference,Site\n" + "\n".join(",".join(row) for row in rows) + "\n"
    workspace_id = fixture.freeze_files({"products": data.encode()})
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
    dataset = selection.datasets[0]
    reference, site = (column.stable_key for column in dataset.columns)
    draft = context.mapping_workspace.save_working_draft(
        workspace_id,
        datasets=(
            DatasetMapping(
                dataset.dataset_id,
                "x.product",
                source_identity_column_keys=(reference, site),
                target_identity=(IdentityComponentMapping((reference,), ("code",)),),
                target_scope=(IdentityComponentMapping((site,), ("site",)),),
            ),
        ),
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

    service._run_live_check(prepared, reader)
    check = repository.get_current_check(workspace_id)
    fixture.assertIsNotNone(
        check, repository.get_check_attempt(workspace_id, prepared.check_id)
    )
    fixture.assertFalse(check.schema_changed)
    return workspace_id, dataset, check, reads
