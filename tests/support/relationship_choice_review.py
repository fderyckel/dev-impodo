"""Publish real relationship checks over accepted fictional source files."""

from datetime import UTC, datetime

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    RelationshipMapping,
    RelationshipResolver,
    ReferenceKeyMapping,
    ResolverOrigin,
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


def relationship_choice_workspace(fixture, *, missing_count=105):
    rows = [
        (f"R{index}", f"M{index:03d}", "BE", "TAGMISS; TAGMISS" if index == 0 else "")
        for index in range(missing_count)
    ]
    rows += [
        ("EXACT", "EXIST", "BE", "TAGOK; tagcase; TAGMISS"),
        ("IN", "INCOMING", "BE", ""),
        ("DUP", "DUP", "BE", ""),
        ("CASE", "lower", "BE", ""),
        ("DUPIN", "DUPIN", "BE", ""),
        ("CASEIN", "lowerin", "BE", ""),
        ("PARTIAL", "", "BE", ""),
        ("BLANK", "", "", ""),
        ("OTHER", "EXIST", "LU", ""),
    ]
    owners = (
        "Reference,Category,Site,Tags\n"
        + "\n".join(",".join(row) for row in rows)
        + "\n"
    )
    parents = "Reference,Site\nEXIST,BE\nEXIST,BE\nINCOMING,BE\nDUP,BE\nlower,BE\nDUPIN,BE\nDUPIN,BE\nLOWERIN,BE\n"
    workspace_id = fixture.freeze_files(
        {"products": owners.encode(), "categories": parents.encode()}
    )

    def field(name, label, kind="char", relation=None, required=False):
        return SchemaField(name, label, kind, required, False, relation, None, ())

    models = (
        SchemaModel(
            "x.product",
            "Product",
            (
                field("code", "Reference", required=True),
                field("category_id", "Category", "many2one", "x.category", True),
                field("tag_ids", "Tags", "many2many", "x.tag"),
            ),
        ),
        SchemaModel(
            "x.category",
            "Category",
            (field("code", "Reference", required=True), field("site", "Site")),
        ),
        SchemaModel("x.tag", "Tag", (field("code", "Reference", required=True),)),
    )
    schema = fixture.schema(
        workspace_id,
        extra_models=models,
        extra_keys=(
            BusinessKeyDefinition(
                "x.product:code",
                "x.product",
                ("code",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "x.category:site-code",
                "x.category",
                ("code",),
                scope_fields=("site",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
            BusinessKeyDefinition(
                "x.tag:code", "x.tag", ("code",), status=BusinessKeyStatus.CONFIRMED
            ),
        ),
    )
    context, actor = fixture.context, fixture.actor
    selection = context.queries.get_mapping_source_selection(workspace_id)
    by_name = {item.name: item for item in selection.datasets}
    owner, parent = by_name["products"], by_name["categories"]
    reference, category, site, tags = (column.stable_key for column in owner.columns)
    parent_ref, parent_site = (column.stable_key for column in parent.columns)
    draft = context.mapping_workspace.save_working_draft(
        workspace_id,
        datasets=(
            DatasetMapping(
                owner.dataset_id,
                "x.product",
                source_identity_column_keys=(reference,),
                target_identity=(IdentityComponentMapping((reference,), ("code",)),),
                relationships=(
                    RelationshipMapping(
                        "category_id",
                        "many2one",
                        (category, site),
                        required=True,
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_THEN_DATASET,
                            model="x.category",
                            dataset_id=parent.dataset_id,
                            key_mappings=(ReferenceKeyMapping(category, "code"),),
                            scope_mappings=(ReferenceKeyMapping(site, "site"),),
                        ),
                    ),
                    RelationshipMapping(
                        "tag_ids",
                        "many2many",
                        (tags,),
                        separator=";",
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_CATALOG,
                            model="x.tag",
                            key_mappings=(ReferenceKeyMapping(tags, "code"),),
                        ),
                    ),
                ),
            ),
            DatasetMapping(
                parent.dataset_id,
                "x.category",
                source_identity_column_keys=(parent_ref, parent_site),
                target_identity=(IdentityComponentMapping((parent_ref,), ("code",)),),
                target_scope=(IdentityComponentMapping((parent_site,), ("site",)),),
            ),
        ),
        expected_version=None,
        actor=actor,
    )
    service = context.matching_order
    prepared = run_local_operation_sync(
        service.prepare_live_check,
        workspace_id,
        selection,
        schema,
        context.queries.get_schema_governance(workspace_id),
        draft,
        service.recommend(selection, schema, draft.definition),
        actor=actor,
    )
    now = datetime.now(UTC)
    service._preferences.begin_check(
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
        requested = {
            request.model: request.fields for request in requirements.metadata_requests
        }
        metadata = metadata_for_schema(schema, requested)
        categories = tuple(
            TargetRecord("x.category", 79000001 + index, {"code": code, "site": scope})
            for index, (code, scope) in enumerate(
                (
                    ("EXIST", "BE"),
                    ("EXIST", "LU"),
                    ("DUP", "BE"),
                    ("DUP", "BE"),
                    ("LOWER", "BE"),
                )
            )
        )
        tag_records = tuple(
            TargetRecord("x.tag", 79000050 + index, {"code": code})
            for index, code in enumerate(("TAGOK", "TAGCASE"))
        )
        records = {"x.category": categories, "x.tag": tag_records, "x.product": ()}
        return metadata, RecordSnapshot(
            metadata.fingerprint,
            {model: records[model] for model in requested},
            requested,
        )

    service._run_live_check(prepared, reader)
    check = service._preferences.get_current_check(workspace_id)
    fixture.assertIsNotNone(
        check, service._preferences.get_check_attempt(workspace_id, prepared.check_id)
    )
    fixture.assertFalse(check.schema_changed)
    return workspace_id, owner, parent, check, reads
