from __future__ import annotations

from datetime import UTC, datetime
import unittest
from uuid import uuid4

from impodo.application.workspace.mapping.order_service import MatchingOrderService
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
)
from impodo.domain.matching_order import (
    MatchingOrderCheckStatus,
    MatchingOrderRelationshipOutcome,
)
from impodo.domain.odoo.contracts import MetadataSnapshot, RecordSnapshot
from impodo.domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
    SchemaGovernance,
)
from impodo.domain.shared.access import CapabilityAuthorizationPolicy, LOCAL_ACTOR
from impodo.domain.shared.models import (
    FieldMetadata,
    ModelMetadata,
    TargetFingerprint,
    TargetRecord,
)
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


class _Keys:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def source_key_tuples(self, workspace_id, dataset_id, source_column_keys):
        self.calls.append((workspace_id, dataset_id, source_column_keys))
        return self.rows[(dataset_id, source_column_keys)]


class _Repository:
    def __init__(self) -> None:
        self.check = None
        self.protected = ""
        self.attempts = []

    def get_preference(self, _workspace_id):
        return None

    def update_check_attempt(self, _workspace_id, attempt):
        self.attempts.append(attempt)

    def publish_check(
        self,
        _workspace_id,
        check,
        *,
        protected_snapshot_json,
        actor,
    ):
        self.check = check
        self.protected = protected_snapshot_json
        return MatchingOrderCheckStatus.SUCCEEDED

    def fail_check(self, _workspace_id, attempt, *, actor):
        raise AssertionError(attempt.failure_message)


class MatchingOrderLiveCheckTests(unittest.TestCase):
    def test_bounded_plan_removes_edge_only_when_all_keys_are_in_odoo(self) -> None:
        evidence = _evidence()
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (("P001",), ("P002",)),
                ("dataset:article", ("column:article-code",)): (
                    ("P001",),
                    ("P002",),
                ),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
            tie_break_order=("dataset:bom", "dataset:article"),
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            tie_break_order=("dataset:bom", "dataset:article"),
            actor=LOCAL_ACTOR,
        )
        captured_requests = []

        def reader(requirements):
            captured_requests.append(requirements)
            metadata = MetadataSnapshot(
                fingerprint=evidence.fingerprint,
                models={
                    "product.template": ModelMetadata(
                        model="product.template",
                        description="Product",
                        fields={
                            "ref": FieldMetadata(name="ref", type="char")
                        },
                    )
                },
            )
            return metadata, RecordSnapshot(
                fingerprint=evidence.fingerprint,
                records={
                    "product.template": (
                        TargetRecord("product.template", 71, {"ref": "P001"}),
                        TargetRecord("product.template", 72, {"ref": "P002"}),
                    )
                },
                requested_fields={"product.template": ("ref",)},
            )

        service._run_live_check(prepared, reader)

        self.assertEqual(len(captured_requests), 1)
        plan = captured_requests[0]
        self.assertEqual(
            plan.metadata_requests[0].fields,
            ("ref",),
        )
        self.assertEqual(len(plan.record_requests), 1)
        self.assertEqual(
            plan.record_requests[0].domain,
            (["ref", "in", ["P001", "P002"]],),
        )
        self.assertEqual(len(keys.calls), 2)
        self.assertEqual(
            repository.check.relationship_results[0].outcome,
            MatchingOrderRelationshipOutcome.TARGET,
        )
        self.assertEqual(
            repository.check.ordered_dataset_ids,
            ("dataset:bom", "dataset:article"),
        )
        self.assertNotIn("P001", repository.check.to_json())
        self.assertNotIn('"id":71', repository.check.to_json())
        self.assertIn("P001", repository.protected)
        self.assertIn('"id":71', repository.protected)

    def test_schema_change_stops_before_relationship_classification(self) -> None:
        evidence = _evidence()
        keys = _Keys(
            {
                ("dataset:bom", ("column:article",)): (("P001",),),
                ("dataset:article", ("column:article-code",)): (("P001",),),
            }
        )
        repository = _Repository()
        service = MatchingOrderService(
            repository,
            CapabilityAuthorizationPolicy(),
            keys,
        )
        local = service.recommend(
            evidence.selection,
            evidence.schema,
            evidence.draft.definition,
        )
        prepared = service.prepare_live_check(
            evidence.workspace_id,
            evidence.selection,
            evidence.schema,
            evidence.governance,
            evidence.draft,
            local,
            actor=LOCAL_ACTOR,
        )

        service._run_live_check(
            prepared,
            lambda _requirements: (
                MetadataSnapshot(
                    fingerprint=evidence.fingerprint,
                    models={
                        "product.template": ModelMetadata(
                            model="product.template",
                            description="Product",
                            fields={
                                "ref": FieldMetadata(
                                    name="ref",
                                    type="char",
                                    required=True,
                                )
                            },
                        )
                    },
                ),
                RecordSnapshot(
                    fingerprint=evidence.fingerprint,
                    records={"product.template": ()},
                    requested_fields={"product.template": ("ref",)},
                ),
            ),
        )

        self.assertTrue(repository.check.schema_changed)
        self.assertEqual(repository.check.relationship_results, ())
        self.assertEqual(repository.check.unchecked_relationship_count, 1)
        self.assertEqual(
            repository.check.ordered_dataset_ids,
            local.ordered_dataset_ids,
        )


class _Evidence:
    pass


def _evidence():
    result = _Evidence()
    result.workspace_id = str(uuid4())
    result.fingerprint = TargetFingerprint(
        target_hash="sha256:" + "7" * 64,
        connection_mode="REMOTE",
        database="odoo",
        odoo_version="19.0",
        snapshot_timestamp="2026-09-10T10:00:00+00:00",
    )
    now = datetime.now(UTC)
    article = _source(
        "dataset:article",
        "articles",
        ("column:article-code",),
    )
    bom = _source(
        "dataset:bom",
        "boms",
        ("column:bom", "column:article"),
    )
    result.selection = SourceSelection(
        selection_id=str(uuid4()),
        version=1,
        data_version_id=str(uuid4()),
        created_at=now,
        created_by="Data manager",
        datasets=(bom, article),
        content_hash="sha256:" + "a" * 64,
    )
    char = SchemaField(
        name="ref",
        label="Reference",
        type="char",
        required=False,
        readonly=False,
        relation=None,
        relation_field=None,
        selection=(),
    )
    result.schema = OdooSchemaCatalog(
        workspace_id=result.workspace_id,
        policy_hash="sha256:" + "1" * 64,
        captured_at=now,
        captured_by="Data manager",
        connection_mode="REMOTE",
        database="odoo",
        odoo_version="19.0",
        models=(
            SchemaModel("product.template", "Product", (char,)),
            SchemaModel(
                "mrp.bom",
                "BOM",
                (
                    SchemaField(
                        name="code",
                        label="Code",
                        type="char",
                        required=False,
                        readonly=False,
                        relation=None,
                        relation_field=None,
                        selection=(),
                    ),
                    SchemaField(
                        name="product_tmpl_id",
                        label="Product",
                        type="many2one",
                        required=False,
                        readonly=False,
                        relation="product.template",
                        relation_field=None,
                        selection=(),
                    ),
                ),
            ),
        ),
        content_hash="sha256:" + "2" * 64,
        origin=SchemaOrigin.LIVE_API,
        read_credential_binding_hash="sha256:" + "3" * 64,
        read_principal_hash="sha256:" + "4" * 64,
        read_permission_hash="sha256:" + "5" * 64,
        read_context_hash="sha256:" + "6" * 64,
        connection_target_hash=result.fingerprint.target_hash,
    )
    result.governance = SchemaGovernance(
        governance_id=str(uuid4()),
        version=1,
        workspace_id=result.workspace_id,
        catalog_hash=result.schema.content_hash,
        permitted_models=("mrp.bom", "product.template"),
        business_keys=(
            BusinessKeyDefinition(
                key_id="product:ref",
                model="product.template",
                key_fields=("ref",),
                status=BusinessKeyStatus.CONFIRMED,
            ),
        ),
        recorded_at=now,
        recorded_by="Data manager",
    )
    definition = MappingDefinition(
        mapping_id=str(uuid4()),
        source_selection_hash=result.selection.content_hash,
        schema_hash=result.governance.content_hash,
        datasets=(
            DatasetMapping(
                dataset_id=bom.dataset_id,
                target_model="mrp.bom",
                source_identity_column_keys=("column:bom",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("column:bom",),
                        target_fields=("code",),
                    ),
                ),
                relationships=(
                    RelationshipMapping(
                        target_field="product_tmpl_id",
                        kind="many2one",
                        source_column_keys=("column:article",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_THEN_DATASET,
                            dataset_id=article.dataset_id,
                            model="product.template",
                            key_mappings=(
                                ReferenceKeyMapping(
                                    source_column_key="column:article",
                                    target_field="ref",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            DatasetMapping(
                dataset_id=article.dataset_id,
                target_model="product.template",
                source_identity_column_keys=("column:article-code",),
                target_identity=(
                    IdentityComponentMapping(
                        source_column_keys=("column:article-code",),
                        target_fields=("ref",),
                    ),
                ),
            ),
        ),
    )
    result.draft = MappingWorkingDraft(
        mapping_id=definition.mapping_id,
        version=1,
        workspace_id=result.workspace_id,
        base_mapping_version=None,
        definition=definition,
        updated_at=now,
        updated_by="Data manager",
    )
    return result


def _source(
    dataset_id: str,
    name: str,
    columns: tuple[str, ...],
) -> SourceDataset:
    return SourceDataset(
        dataset_id=dataset_id,
        name=name,
        source=FileSourceBinding(
            file_id=f"file:{dataset_id}",
            table_key=name,
            source_sha256="sha256:" + "a" * 64,
            catalog_hash="sha256:" + "b" * 64,
            encoding="utf-8",
            delimiter=",",
            header_row=1,
        ),
        row_count=2,
        columns=tuple(
            SourceDatasetColumn(index, key, key, "string")
            for index, key in enumerate(columns, start=1)
        ),
    )


if __name__ == "__main__":
    unittest.main()
