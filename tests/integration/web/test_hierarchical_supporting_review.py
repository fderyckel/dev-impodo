"""Persisted supporting reviews preserve separate table-owned hierarchy rules."""

from dataclasses import asdict, replace
from uuid import uuid4

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.domain.mapping.contracts import RelationshipResolver, ResolverOrigin
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.support.hierarchy_workflow import HierarchyWorkflowFixture


class HierarchicalSupportingReviewTests(HierarchyWorkflowFixture):
    def schema(self, workspace_id, **options):
        schema = super().schema(workspace_id, **options)
        models = tuple(
            replace(
                model,
                fields=(
                    *model.fields,
                    SchemaField(
                        "child_id",
                        "Children",
                        "one2many",
                        False,
                        False,
                        "product.category",
                        "parent_id",
                        (),
                    ),
                ),
            )
            if model.name == "product.category"
            else model
            for model in schema.models
        )
        schema = replace(
            schema,
            models=models,
            content_hash=content_hash([asdict(model) for model in models]),
        )
        governance = self.context.queries.get_schema_governance(workspace_id)
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=self.actor
        )
        self.context.schema_workspace.govern(
            workspace_id, actor=self.actor, business_keys=governance.business_keys
        )
        return schema

    def test_authoring_and_preparation_accept_separate_parent_providers_for_one_model(
        self,
    ):
        for major in (19, 20):
            for mode in ("LOCAL", "REMOTE"):
                with self.subTest(major=major, mode=mode):
                    workspace, _validation = self.hierarchy(
                        major=major, mode=mode, check_only=True
                    )
                    packet = self.context.schema_workspace.supporting_review_inputs(
                        workspace, actor=self.actor
                    )
                    review = run_local_operation_sync(
                        self.context.schema_workspace.supporting.save,
                        packet,
                        expected_binding_hash=packet.binding_hash,
                        expected_version=0,
                        operation_id=str(uuid4()),
                        choices=(),
                        actor=self.actor,
                    )
                    plan = self.context.queries.get_derived_entity_plan(workspace)
                    link = derived_dataset_links(plan)[0]
                    draft = self.context.queries.get_mapping_working_draft(workspace)
                    self.assertIsNotNone(draft)
                    generated = next(
                        item
                        for item in draft.definition.datasets
                        if item.dataset_id == link.derived_dataset_id
                    )
                    original = next(
                        item
                        for item in draft.definition.datasets
                        if item.dataset_id != link.derived_dataset_id
                    )
                    # Two independently authored source tables target the same
                    # nested model, with distinct and explicit parent providers.
                    second = replace(
                        generated,
                        dataset_id=original.dataset_id,
                        source_identity_column_keys=original.source_identity_column_keys,
                        target_identity=original.target_identity,
                        target_scope=(
                            replace(
                                generated.target_scope[0],
                                resolver=RelationshipResolver(
                                    ResolverOrigin.TARGET_CATALOG,
                                    model=generated.target_model,
                                ),
                            ),
                        ),
                    )
                    self._require_rules(workspace, (generated, second), packet, review)
                    # Both tables can also supply children to the same incoming
                    # parent table, including its reverse Children collection.
                    self._require_rules(
                        workspace,
                        (
                            generated,
                            replace(second, target_scope=generated.target_scope),
                        ),
                        packet,
                        review,
                    )
                    self.assertEqual(
                        self.context.queries.get_mapping_working_draft(workspace), draft
                    )
                    self.assertEqual(
                        self.context.schema_workspace.supporting.current(
                            workspace, actor=self.actor
                        ),
                        review,
                    )

    def _require_rules(self, workspace, mappings, packet, review):
        previous_calls = len(self.readiness_calls)
        with collect_duckdb_request_timings() as timings:
            actual = run_local_operation_sync(
                self.context.mapping_workspace.supporting_decisions.require_mapping,
                workspace,
                mappings,
                packet.schema,
                packet.governance,
            )
        self.assertEqual(actual, review)
        # Registry, source package and workspace evidence retain
        # their existing bounded local ownership; no row scan occurs.
        self.assertLessEqual(timings.connection_count, 4)
        self.assertLessEqual(timings.schema_check_count, 4)
        self.assertEqual(timings.lock_retry_count, 0)
        run_local_operation_sync(
            self.context.mapping_workspace.supporting_decisions.require_preparation,
            workspace,
            mappings,
            self.context.schema_workspace.schemas,
            packet.workspace,
            packet.selection,
            packet.preparation,
        )
        self.assertEqual(len(self.readiness_calls), previous_calls)
