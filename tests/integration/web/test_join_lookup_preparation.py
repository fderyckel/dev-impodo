"""Prepare the reported join-plus-generated-list shape and reuse its Recipe."""

from dataclasses import replace
from unittest import TestCase

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
)
from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.mapping.validation.evidence import mapping_issue_fingerprint
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import SchemaField
from impodo.domain.workspace.derived_entities import derived_dataset_links
from impodo.web.composition.page_reads import run_local_operation_sync
from tests.integration.web import test_mixed_join_preparation as fixtures
from tests.support.browser_scenarios import POST_HEADERS, _wait_for_preparation


def delivery(row_counts=(4753, 44387, 16159), *, offset=0):
    """Keep real blank lookup values in both the first and fresh deliveries."""
    result = []
    for name, payload in fixtures.delivery(row_counts, offset=offset):
        if name == "cust01.csv":
            lines = payload.decode().splitlines()
            for index in range(4, len(lines), 4):
                cells = lines[index].split(",")
                cells[2] = ""
                lines[index] = ",".join(cells)
            payload = ("\n".join(lines) + "\n").encode()
        result.append((name, payload))
    return result


class JoinLookupPreparationTests(TestCase):
    def setUp(self):
        base = fixtures.MixedJoinPreparationTests(methodName="runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.fixture, self.context, self.actor = base.fixture, base.context, base.actor

    def _schema(self, workspace_id):
        schema = fixtures.MixedJoinPreparationTests._schema(self, workspace_id)
        keys = self.context.schema_workspace.schemas.get_schema_governance(
            workspace_id
        ).business_keys
        models = tuple(
            replace(
                model,
                fields=(
                    *model.fields,
                    SchemaField(
                        "x_generated_id",
                        "Generated label",
                        "many2one",
                        False,
                        False,
                        "x_secondary",
                        None,
                        (),
                    ),
                ),
            )
            if model.name == "x_record"
            else model
            for model in schema.models
        )
        schema = replace(
            schema,
            models=models,
            content_hash=content_hash((schema.content_hash, "generated-label")),
        )
        self.context.schema_workspace.schemas.save_odoo_schema_catalog(
            workspace_id, schema, actor=self.actor
        )
        self.context.schema_workspace.govern(
            workspace_id,
            business_keys=keys,
            actor=self.actor,
        )
        return schema

    def _setup(self, row_counts=(4753, 44387, 16159)):
        context, actor = self.context, self.actor
        workspace_id, physical, by_name = self.fixture._frozen_customer_workspace(
            source_files=delivery(row_counts)
        )
        self._schema(workspace_id)
        primary, details, addresses = (
            by_name[name]
            for name in ("customer_accounts", "party_details", "customer_addresses")
        )
        column = self.fixture._column
        request = ExactJoinGroupRequest(
            output_dataset_name="records_complete",
            primary_dataset_id=primary.dataset_id,
            primary_key_column_keys=(column(primary, "Party").stable_key,),
            key_value_types=("integer",),
            additions=tuple(
                ExactJoinGroupAddition(
                    dataset.dataset_id, (column(dataset, key).stable_key,)
                )
                for dataset, key in ((details, "RecId"), (addresses, "Party"))
            ),
            projections=(
                ExactJoinGroupProjection(
                    primary.dataset_id, column(primary, "AccountNum").stable_key, "ID"
                ),
                ExactJoinGroupProjection(
                    details.dataset_id, column(details, "Name").stable_key, "Choice"
                ),
            ),
        )
        run_local_operation_sync(
            context.derived_entities.save_join_group,
            workspace_id,
            request,
            expected_parent_version=None,
            actor=actor,
        )
        plan, _ = run_local_operation_sync(
            context.derived_entities.save_rule,
            workspace_id,
            output_dataset_name="generated_labels",
            source_dataset_id=details.dataset_id,
            source_column_key=column(details, "Name").stable_key,
            target_model="x_secondary",
            target_name_field="ref",
            external_id_namespace="custom_fixture",
            parent_separator=None,
            blank_policy="quarantine",
            expected_parent_version=1,
            actor=actor,
        )
        effective = context.queries.get_mapping_source_selection(workspace_id)
        lookup = next(d for d in effective.datasets if d.name == "generated_labels")
        output = next(d for d in effective.datasets if d.name == "records_complete")
        link = derived_dataset_links(plan)[0]
        columns = {c.source_name: c.stable_key for c in output.columns}
        mappings = (
            DatasetMapping(
                dataset_id=lookup.dataset_id,
                target_model="x_secondary",
                source_identity_column_keys=(link.canonical_key_column_key,),
                target_identity=(
                    IdentityComponentMapping(
                        (link.name_column_key,), ("ref",), "string"
                    ),
                ),
            ),
            DatasetMapping(
                dataset_id=output.dataset_id,
                target_model="x_record",
                source_identity_column_keys=(columns["ID"],),
                target_identity=(
                    IdentityComponentMapping((columns["ID"],), ("ref",), "string"),
                ),
                relationships=(
                    RelationshipMapping(
                        target_field="x_generated_id",
                        kind="many2one",
                        categorical_policy=CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
                        source_column_keys=(columns["Choice"],),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.DATASET, dataset_id=lookup.dataset_id
                        ),
                    ),
                    RelationshipMapping(
                        target_field="x_list_id",
                        kind="many2one",
                        source_column_keys=(columns["Choice"],),
                        categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
                        blank_source_values=("TO_DEFINE",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.TARGET_CATALOG,
                            model="x_list",
                            key_mappings=(
                                ReferenceKeyMapping(columns["Choice"], "ref"),
                            ),
                            value_mappings=(
                                ValueMapping("A", "Alpha"),
                                ValueMapping("B", "Beta"),
                            ),
                        ),
                    ),
                ),
            ),
        )
        with collect_duckdb_request_timings() as timings:
            revision, validation = run_local_operation_sync(
                context.mapping_workspace.check_definition,
                workspace_id,
                datasets=mappings,
                expected_parent_version=None,
                expected_working_draft_version=None,
                actor=actor,
            )
        self.assertLessEqual(timings.connection_count, 5)
        self.assertLessEqual(timings.schema_check_count, 1)
        self.assertNotEqual(validation.status.value, "INVALID", validation.issues)
        draft = context.mapping_workspace.mappings.get_mapping_working_draft(
            workspace_id
        )
        run_local_operation_sync(
            context.mapping_workspace.submit_current,
            workspace_id,
            datasets=revision.definition.datasets,
            expected_version=revision.version,
            expected_working_draft_version=draft.version,
            warning_acknowledgements=tuple(
                mapping_issue_fingerprint(i)
                for i in validation.issues
                if i.severity == "warning"
            ),
            actor=actor,
        )
        self.assertEqual(context.derived_entities.get_derived_entity_plan(workspace_id).content_hash, plan.content_hash)
        return workspace_id, row_counts[0]

    def _worker(self, workspace_id, count):
        started = self.fixture.client.post(
            f"/workspaces/{workspace_id}/summary/check",
            data={"csrf_token": self.fixture.csrf},
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(started.status_code, 303, started.text)
        result = _wait_for_preparation(
            self.fixture.client, started.headers["location"], timeout=120
        )
        self.assertEqual(result["status"], "SUCCEEDED", result)
        quality = self.context.quality.current_run(workspace_id)
        self.assertEqual(quality.ready_count, count + 3)
        self.assertEqual(quality.blocked_count, 0)
        self.assertEqual(len(quality.source_accounting), 65299)
        summary = self.context.preparation.staging.get_current_staging_summary(
            workspace_id
        )
        run = self.context.preparation.staging.get_canonical_staging_run(
            workspace_id, summary.run_id, expected_content_hash=summary.content_hash
        )
        records = tuple(row for row in run.rows if row.dataset == "records_complete")
        self.assertEqual(len(records), count)
        self.assertEqual(
            {row.references["x_generated_id"].key for row in records if row.references["x_generated_id"] is not None},
            {("a",), ("b",), ("to_define",)},
        )
        self.assertEqual(
            sum(row.references["x_list_id"] is None for row in records),
            sum(index % 4 == 0 or index % 3 == 2 for index in range(1, count + 1)),
        )
        self.assertEqual(sum(row.references["x_generated_id"] is None for row in records), count // 4)
        generated = tuple(row for row in run.rows if row.dataset == "generated_labels")
        self.assertEqual({row.target_identity for row in generated}, {("A",), ("B",), ("TO_DEFINE",)})
        self.assertEqual(len(generated), 3)
        self.assertEqual(
            sum(
                len(rows)
                for row in generated
                for rows in row.lineage.physical_sources.values()
            ),
            44387 - 44387 // 4,
        )
        normalization = self.context.normalization.current_summary(workspace_id)
        approved = self.fixture.client.post(
            f"/workspaces/{workspace_id}/normalization/approve",
            data={
                "csrf_token": self.fixture.csrf,
                "run_id": normalization.run_id,
                "lifecycle_version": str(normalization.lifecycle_version),
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        self.assertTrue(self.context.normalization.current_summary(workspace_id).frozen)
        review = self.fixture.client.get(f"/workspaces/{workspace_id}/summary")
        self.assertIn("Compare the approved data with Odoo", review.text)
        return run

    def test_65299_rows_join_and_generated_list_reach_final_review_and_recipe_reuse(
        self,
    ):
        workspace_id, count = self._setup()
        self._worker(workspace_id, count)
        fixtures.MixedJoinPreparationTests._reuse(
            self, workspace_id, count, stable_lookup=True, delivery_factory=delivery
        )
